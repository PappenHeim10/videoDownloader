"""Fetching the tracks a selection named, and combining them into one file.

Two fetch paths, chosen by the source's own `source_type`, so nothing here has
to know which website a job came from:

* `HTTP` goes through the engine's progressive transport, which owns resume,
  retries and atomic finalisation.
* `YTDLP` goes through the resolver that produced the URL.

  It used to be able to leave that path: one request for the last byte proved
  whether our own transport could read the whole file, and a track that passed
  was fetched by the engine instead, for the resume, retries and atomic
  finalisation the engine owns. That routing is switched off - see
  `ROUTE_RESOLVER_TRACKS_TO_ENGINE`, which carries the measurement that turned
  it off and the condition for turning it back on. The probe and the conversion
  are kept for that day; with the switch off the probe is not even sent, because
  the question it answers is one nobody asks any more.

Progress is aggregated across every phase - each track, then the mux - so the
bar moves once from zero to done rather than restarting per file.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from base_api.models import MediaSource
from base_api.modules.config import DownloadConfigHTTP
from base_api.modules.errors import UnsupportedProtocolError

from video_downloader.application.muxing import (
    ContainerChoice,
    choose_container,
    mux_tracks,
)
from video_downloader.application.track_selection import TrackSelection, codec_family

logger = logging.getLogger(__name__)

#: The transport a source whose bytes are fetched by the resolver that produced
#: it. Named after what it is - a transport - because that is the question
#: `source_type` answers; a provider name here would be a layering violation.
YTDLP_TRANSPORT = "YTDLP"

#: What a 206 states about the part it served: `bytes 0-0/10240`. A tail read
#: is asked without naming the byte - `Range: bytes=-1` - so this header is
#: where the length comes from, and its numbers are checked rather than trusted:
#: the byte served has to be the last one of the resource. A server answering
#: `bytes */*`, or answering a suffix range with its first byte, has not said
#: what was asked.
_CONTENT_RANGE = re.compile(r"\Abytes\s+(\d+)-(\d+)/(\d+)\Z")

#: Above this, a download is worth asking about. A 2160p60 VP9 track measured
#: 1 362 269 481 bytes, so "one click, 1.4 GB, no warning" is a real sequence.
LARGE_DOWNLOAD_BYTES = 2 * 1024**3

#: Whether a resolver-resolved track may be fetched by the engine transport when
#: a probe proves it can finish. **Measured off on 2026-09-13.**
#:
#: The routing was built for what the engine owns and the resolver does not:
#: resume, retries, atomic finalisation. What it costs was never measured until
#: a user reported a slow download. On one YouTube track - format 313, 2160p
#: vp9, 311 597 023 bytes - on one machine within the same few minutes:
#:
#:     engine transport      556 KB/s
#:     yt-dlp's downloader   the whole 297 MiB in under 35 s, 12-27 MiB/s
#:
#: At least sixteen times slower. Resume is worth a great deal, and it is not
#: worth an order of magnitude: a two-hour stream turns from minutes into hours,
#: which is its own kind of unreliability.
#:
#: Turn back on, and **re-measure**, once the progressive transport in
#: `eaf_base_api` is fixed - the bottleneck is there rather than here, and
#: nothing in this repository can fix it. `readable_total` and
#: `as_engine_source` are kept against that day.
ROUTE_RESOLVER_TRACKS_TO_ENGINE = False


class TrackDownloadError(RuntimeError):
    """One track of a multi-track download failed.

    Carries which one, because the answer differs: a finished track is kept and
    a retry costs one download rather than two.
    """

    def __init__(self, role: str, cause: BaseException) -> None:
        self.role = role
        self.cause = cause
        super().__init__(f"The {role} track could not be downloaded: {cause}")


class DownloadTooLargeError(RuntimeError):
    """The estimate exceeded the configured ceiling; nothing was transferred."""


@dataclass
class _Phase:
    """One unit of work in an aggregated progress bar."""

    name: str
    weight: int
    done: int = 0
    #: Diese Phase hat keine eigene Groesse - sie wiegt, was die anderen wiegen.
    #: Das Muxen ist die einzige: es liest beide Spuren, also kostet es
    #: ungefaehr, was beide zusammen wiegen, und das steht erst fest, wenn beide
    #: gewogen sind.
    derived: bool = False


class _AggregateProgress:
    """One progress bar over several phases, monotonic by construction.

    Each phase reports its own bytes from zero; the total is the sum of every
    phase's weight, and a phase that finishes short still counts as complete so
    the bar never goes backwards when the next one starts.

    A phase whose provider stated no size starts weightless, and there is no
    honest bar to draw until the download itself learns a total - so that one is
    adopted when it arrives. Phases that *were* estimated keep their weight even
    when the wire disagrees, which is what stops one of them pushing the bar past
    a hundred percent.
    """

    def __init__(self, phases: list[_Phase], report: Callable[[int, int], None]) -> None:
        self._phases = phases
        self._report = report

    def callback_for(self, name: str) -> Callable[[int, int], None]:
        phase = next(phase for phase in self._phases if phase.name == name)

        def callback(done: int, total: int) -> None:
            if not phase.weight and total > 0:
                # Nobody could estimate this one; the download just measured it.
                phase.weight = total
            # A phase may learn its real size mid-flight; an estimated weight
            # stays what it was, so one phase cannot push the bar past 100%.
            phase.done = min(done, phase.weight) if phase.weight else done
            self._emit()

        return callback

    def complete(self, name: str) -> None:
        for phase in self._phases:
            if phase.name == name:
                if not phase.weight:
                    # Niemand hat sie vorher gewogen; was ankam, ist jetzt die
                    # einzige belegte Zahl und damit ihr Gewicht.
                    phase.weight = phase.done
                phase.done = phase.weight
        self._emit()

    def _denominator(self) -> int:
        """Der Gesamtwert - oder 0, solange eine Phase ungewogen ist.

        Frueher war der Nenner die Summe der bereits bekannten Gewichte. Das
        las sich harmlos und lief messbar rueckwaerts: bei zwei ungewogenen
        Spuren stand der Balken auf 100 %, sobald die erste fertig war, und fiel
        auf 87,5 %, sobald die zweite ihre Groesse lernte.

        Eine Null ist hier keine fehlende Angabe, sondern eine Aussage: sie
        heisst "Ende unbekannt", genau wie `DownloadJob.has_known_total` sie
        liest, und die Oberflaeche zeichnet dafuer einen unbestimmten Balken
        statt eines Prozentsatzes, der sich noch verschiebt.
        """
        measured = [phase for phase in self._phases if not phase.derived]
        if not measured or any(phase.weight <= 0 for phase in measured):
            return 0
        base = sum(phase.weight for phase in measured)
        for phase in self._phases:
            if phase.derived:
                phase.weight = base
        return base + sum(phase.weight for phase in self._phases if phase.derived)

    def _emit(self) -> None:
        total = self._denominator()
        done = sum(phase.done for phase in self._phases)
        self._report(min(done, total) if total else done, total)


def is_ytdlp(source: MediaSource) -> bool:
    return source.source_type == YTDLP_TRANSPORT


def estimate_bytes(selection: TrackSelection) -> int | None:
    """What the selection is expected to transfer, or `None` if unknowable."""
    sizes = [source.expected_size for source in selection.sources]
    if not sizes or any(size is None for size in sizes):
        return None
    return sum(size for size in sizes if size is not None)


def container_for(selection: TrackSelection) -> ContainerChoice:
    """The container the finished file will be in."""
    if selection.combined is not None:
        container = (selection.combined.track.container or "mp4").strip().lower()
        return ContainerChoice(
            "matroska" if container == "mkv" else container, f".{container}"
        )
    return choose_container(
        codec_family(selection.video.track.video_codec) if selection.video else None,
        codec_family(selection.audio.track.audio_codec) if selection.audio else None,
    )


def _total_from(header: Any) -> int | None:
    """The resource's length, read out of the `Content-Range` of a tail read.

    Verified rather than parsed: the byte served has to be the last one of the
    resource, which is what a suffix range asked for. That check is what makes
    the number safe to hand a downloader as the size to expect - a server that
    answered about some other byte has not answered this question.
    """
    match = _CONTENT_RANGE.match(str(header or "").strip())
    if not match:
        return None
    first, last, total = (int(group) for group in match.groups())
    if first != last or last != total - 1:
        return None
    return total


async def readable_total(source: MediaSource, timeout: float = 15.0) -> int | None:
    """This source's full length, iff our own transport can read every byte.

    One request for one byte, at the far end of the file. It answers the only
    question that matters here and that nothing else can answer offline: some
    media URLs serve a fixed prefix and then refuse, which a download discovers
    as a 403 somewhere in the middle after transferring everything before it.
    Asking for the last byte finds that out for a fraction of a kilobyte.

    Which byte that is depends on whether anybody stated how long the file is:

    * A stated size is asked about by number, exactly as it always has been, so
      a server that disagrees about the total refuses the range rather than
      quietly serving some other byte.
    * A size nobody stated is asked for as a suffix - `Range: bytes=-1`, "the
      last byte, whichever it is" - and the answer states the length. That is
      the only number X ever gives for its own files, and it is the true one:
      measured at 33 007 913 bytes against a `filesize_approx` of 86 514 480,
      and the download transferred 33 007 913.

    Returns `None` for "not through our transport", and every failure answers
    that: a probe must never be the reason a job fails, because the path it
    falls back to already works.
    """
    stated = source.expected_size if (source.expected_size or 0) > 0 else None

    from curl_cffi.requests import AsyncSession

    headers = dict(source.headers)
    headers["Accept-Encoding"] = "identity"
    headers["Range"] = f"bytes={stated - 1}-{stated - 1}" if stated else "bytes=-1"
    try:
        async with AsyncSession() as session:
            response = await session.get(source.url, headers=headers, timeout=timeout)
            if int(response.status_code) != 206:
                return None
            # A stated size needs nothing read back: the 206 says that byte was
            # served, and by construction that byte was the last one.
            return stated or _total_from(response.headers.get("Content-Range"))
    except Exception as error:  # noqa: BLE001 - a probe may never fail a job
        logger.debug("Readability probe failed; keeping the resolver path: %s", error)
        return None


def as_engine_source(source: MediaSource, total: int) -> MediaSource:
    """The same track, marked as one the engine's transport fetches.

    `total` is what the probe measured, and it is carried rather than recomputed:
    for a provider that stated a size it is that size, and for one that stated
    none it is the only length anybody has. The engine needs it to resume, to
    know when it is finished, and to report a bar that starts at a real number.

    A copy rather than a mutation: the selection is shared with the caller and
    with the job's own record of what it chose, and rewriting a field on it
    would change what that record says after the fact.
    """
    return replace(source, source_type="HTTP", expected_size=total)


async def _download_via_engine(
    core: Any,
    source: MediaSource,
    target: Path,
    state_path: Path,
    callback: Callable[[int, int], None],
    stop_event: asyncio.Event,
) -> None:
    configuration = DownloadConfigHTTP(
        quality="best",
        path=str(target),
        callback=callback,
        stop_event=stop_event,
        media_source=source,
        expected_size=source.expected_size,
        state_path=str(state_path),
        # A track that is half downloaded is worth keeping: the other one may
        # already be finished, and discarding both on a stop would make a
        # cancelled two-track job cost twice as much to resume.
        cleanup_on_stop=False,
    )
    await core.download(configuration)


async def _download_via_resolver(
    source: MediaSource,
    target: Path,
    callback: Callable[[int, int], None],
    stop_event: asyncio.Event,
    page_url: str,
) -> None:
    """Fetch one track with yt-dlp, one format selector per call.

    `page_url` is the page the source was resolved from, carried in from the
    `Media` rather than rebuilt here. Rebuilding it would mean knowing how each
    provider spells a watch link, which is exactly the knowledge this layer
    exists without.

    Never a `<video>+<audio>` selector: without ffmpeg on PATH yt-dlp aborts the
    merge *and discards what it downloaded*, which turns a missing tool into a
    lost gigabyte. Tracks are fetched separately and combined by `muxing`.

    Runs in a worker thread because yt-dlp is synchronous; the event loop stays
    free, which is what keeps the UI responsive and the stop event answerable.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadCancelled

    from video_downloader.providers.ytdlp_options import base_options

    identity = source.identity or ""
    format_id = identity.rsplit(":", 1)[-1]
    if not format_id:
        raise UnsupportedProtocolError(
            "A resolver-fetched source needs an identity naming its format."
        )
    if "+" in format_id:
        raise UnsupportedProtocolError(
            "A merged format selector must never be requested; it loses the "
            "already downloaded bytes when no muxer is installed."
        )

    def hook(status: dict) -> None:
        if stop_event.is_set():
            raise DownloadCancelled("the job was stopped")
        if status.get("status") == "downloading":
            callback(
                int(status.get("downloaded_bytes") or 0),
                int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0),
            )

    options = base_options(
        format=format_id,
        outtmpl=str(target),
        progress_hooks=[hook],
        overwrites=True,
        # The URL was resolved moments ago and may have expired since; letting
        # yt-dlp resolve it again is the whole reason this path exists.
        noprogress=True,
    )

    def run() -> None:
        with YoutubeDL(options) as downloader:
            downloader.download([page_url])

    await asyncio.to_thread(run)


async def download_selection(
    selection: TrackSelection,
    *,
    core: Any,
    target: Path,
    work_dir: Path,
    page_url: str,
    stop_event: asyncio.Event,
    report: Callable[[int, int], None],
    on_muxing: Callable[[], None] | None = None,
) -> Path | None:
    """Fetch everything the selection names and leave one file at `target`.

    The tracks land in `work_dir` and the finished file is moved onto `target`
    exactly once, at the end, so an existing download is never replaced by a
    half-written one.

    `page_url` is the media's own page, needed only by the resolver path, which
    re-resolves it to get a URL that has not expired since the selection was
    made.

    Returns `None` when the stop event ended it, which is the same signal the
    engine's own transport gives - a stop is a result, not an exception, and
    `CancelledError` here would mean something else entirely: that the asyncio
    task itself was cancelled.
    """
    if stop_event.is_set():
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    sources = list(selection.sources)
    container = container_for(selection)

    phases = [
        _Phase(name=source.track.role or f"track{index}",
               weight=source.expected_size or 0)
        for index, source in enumerate(sources)
    ]
    if selection.needs_muxing:
        # The mux reads both tracks, so it is worth roughly what they weigh -
        # was erst feststeht, wenn beide gewogen sind. Deshalb abgeleitet statt
        # hier einmal ausgerechnet.
        phases.append(_Phase(name="mux", weight=0, derived=True))
    progress = _AggregateProgress(phases, report)

    paths: dict[str, Path] = {}
    for index, source in enumerate(sources):
        role = source.track.role or f"track{index}"
        extension = (source.track.container or "bin").strip().lower()
        track_path = work_dir / f"{role}.{extension}"
        fetch_source = source
        if ROUTE_RESOLVER_TRACKS_TO_ENGINE and is_ytdlp(source):
            total = await readable_total(source)
            if total is not None:
                # Our own transport owns resume, retries and atomic
                # finalisation, so it is the better place to be whenever it can
                # finish the job - as long as it is not an order of magnitude
                # slower at it. See the switch.
                fetch_source = as_engine_source(source, total)
                logger.info(
                    "Fetching the %s track through the engine transport (%d bytes).",
                    role, total,
                )
        try:
            if is_ytdlp(fetch_source):
                await _download_via_resolver(
                    fetch_source, track_path, progress.callback_for(role),
                    stop_event, page_url,
                )
            else:
                await _download_via_engine(
                    core, fetch_source, track_path, work_dir / f"{role}.state.json",
                    progress.callback_for(role), stop_event,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - named per track for the caller
            if stop_event.is_set():
                return None
            raise TrackDownloadError(role, error) from error
        if stop_event.is_set():
            return None
        progress.complete(role)
        paths[role] = track_path

    if not selection.needs_muxing:
        only = next(iter(paths.values()))
        only.replace(target)
        return target

    if on_muxing is not None:
        on_muxing()
    return await asyncio.to_thread(
        mux_tracks,
        paths["video"],
        paths.get("audio"),
        target,
        container,
        progress.callback_for("mux"),
    )
