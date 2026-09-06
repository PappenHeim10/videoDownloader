"""Putting a video track and an audio track into one container, losslessly.

No re-encoding, ever. Every combination this application selects is one PyAV can
copy packet for packet, and a transcode would turn a download into a CPU-bound
job that silently changes what the user asked for. When no container can hold a
pair, that is a failure to report rather than a reason to re-encode.

The container is chosen from the pair, never the pair from the container - which
is why `track_selection` prefers codecs that share one. What arrives here is
whatever it settled on, including the awkward combinations, and this module's
job is to find the one container that holds them.

Measured with PyAV 18.1.0 / FFmpeg 8.x against real tracks: H.264+AAC, VP9+Opus,
H.264+Opus and VP9+AAC all mux and re-decode. The one refusal is AAC in WebM,
which is why that pair goes to Matroska.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

#: Container to write, and the extension that names it honestly. A file called
#: `.mp4` that is Matroska inside is a lie the user only discovers in a player.
MP4 = ("mp4", ".mp4")
WEBM = ("webm", ".webm")
MATROSKA = ("matroska", ".mkv")

#: Video codec families MP4 carries. Matches the families `track_selection`
#: knows; anything else falls through to Matroska, which carries everything.
_MP4_VIDEO = frozenset({"avc1", "avc3", "av01", "hev1", "hvc1"})
_MP4_AUDIO = frozenset({"mp4a", "opus", "ac-3", "ec-3", "flac"})
_WEBM_VIDEO = frozenset({"vp09", "av01"})
#: Deliberately narrow. WebM refuses AAC outright - PyAV raises
#: `ValueError: 'webm' format does not support 'aac' codec` - and a container
#: that refuses at mux time is better found here than after two downloads.
_WEBM_AUDIO = frozenset({"opus", "vorbis"})

#: How far the two tracks may disagree in length before it is worth saying so.
#: They always disagree a little: a 92-second video measured 91.966 s of video
#: against 92.020 s of audio, because the last frame and the last audio packet
#: do not end together. Neither a bug nor a reason to refuse.
DRIFT_TOLERANCE_SECONDS = 2.0
DRIFT_TOLERANCE_FRACTION = 0.02

_RENAME_RETRY_ATTEMPTS = 5
_RENAME_RETRY_INITIAL_DELAY = 0.1


class MuxError(RuntimeError):
    """The two tracks could not be put into one file.

    Its own type because the caller has to tell it from a download failure: the
    bytes are all there and correct, so the tracks are worth keeping and a retry
    costs nothing but a mux, not another gigabyte.
    """


@dataclass(frozen=True)
class ContainerChoice:
    """Which container holds this pair, and what the file should be called."""

    format_name: str
    extension: str


def choose_container(
    video_codec_family: str | None, audio_codec_family: str | None
) -> ContainerChoice:
    """The container that holds this pair without re-encoding either track.

    MP4 first, because it is what a user expects a video file to be and what
    every player opens. WebM when the pair is the one WebM was defined for.
    Matroska otherwise - it carries every combination, at the cost of a file
    extension some players do not associate with video.

    A video track with no audio keeps its own container: there is nothing to
    reconcile, and re-wrapping it would be work with no result.
    """
    if audio_codec_family is None:
        if video_codec_family in _WEBM_VIDEO and video_codec_family != "av01":
            return ContainerChoice(*WEBM)
        return ContainerChoice(*MP4)

    if video_codec_family in _MP4_VIDEO and audio_codec_family in _MP4_AUDIO:
        return ContainerChoice(*MP4)
    if video_codec_family in _WEBM_VIDEO and audio_codec_family in _WEBM_AUDIO:
        return ContainerChoice(*WEBM)
    return ContainerChoice(*MATROSKA)


def _close_quietly(container: Any, role: str) -> None:
    """Never let a failure to close replace the error being reported."""
    if container is None:
        return
    try:
        container.close()
    except Exception as error:  # noqa: BLE001 - closing must not mask anything
        logger.debug("Closing the %s container failed: %s", role, error)


def _replace_with_retry(source: str, target: str) -> None:
    """Move `source` onto `target`, tolerating a brief Windows sharing violation.

    Deliberately a copy of the engine's helper rather than an import of it: that
    one is private to `BaseCore`, and reaching into another package's internals
    to move a file would be a worse dependency than twenty lines. The behaviour
    has to match, because the cause does - PyAV has just closed these files, and
    Windows can still hold one for a moment afterwards.

    Only WinError 32 is retried. Any other PermissionError means something is
    genuinely wrong and must not be papered over by waiting.
    """
    delay = _RENAME_RETRY_INITIAL_DELAY
    for attempt in range(1, _RENAME_RETRY_ATTEMPTS + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) != 32 or attempt >= _RENAME_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "Mux output blocked by another process; retry %s/%s after %.0f ms",
                attempt + 1, _RENAME_RETRY_ATTEMPTS, delay * 1000,
            )
            time.sleep(delay)
            delay *= 2


def _stream_duration(stream: Any) -> float | None:
    try:
        if stream.duration is None or stream.time_base is None:
            return None
        return float(stream.duration * stream.time_base)
    except (TypeError, ValueError):
        return None


def _warn_on_drift(video_seconds: float | None, audio_seconds: float | None) -> None:
    """Say so when the tracks disagree by more than they always do.

    This is the only place the check can run: neither provider states a duration
    per track, so the honest measurement is the containers themselves, and they
    only exist once both downloads finished.
    """
    if video_seconds is None or audio_seconds is None:
        return
    drift = abs(video_seconds - audio_seconds)
    longest = max(video_seconds, audio_seconds)
    if drift <= DRIFT_TOLERANCE_SECONDS and (
        longest <= 0 or drift / longest <= DRIFT_TOLERANCE_FRACTION
    ):
        return
    logger.warning(
        "Video and audio differ by %.2fs (%.2fs vs %.2fs); muxing anyway.",
        drift, video_seconds, audio_seconds,
    )


def _interleaved(video_input: Any, audio_input: Any, video_stream: Any, audio_stream: Any) -> Iterator[tuple[Any, Any]]:
    """Yield `(packet, source_stream)` in non-decreasing DTS order.

    Writing every video packet and then every audio packet works - FFmpeg's
    muxer buffers and interleaves - but it buffers a whole track to do it. On a
    1 GB video that is a gigabyte of memory to save twenty lines here.
    """
    video_packets = video_input.demux(video_stream)
    audio_packets = audio_input.demux(audio_stream)

    def timestamp(packet: Any) -> float:
        if packet is None or packet.dts is None or packet.time_base is None:
            return float("inf")
        return float(packet.dts * packet.time_base)

    def advance(packets: Iterator[Any]) -> Any:
        for packet in packets:
            # A flush packet carries no data and no timestamps; muxing it is how
            # a stream gets a spurious trailing entry.
            if packet.dts is not None:
                return packet
        return None

    pending_video = advance(video_packets)
    pending_audio = advance(audio_packets)

    while pending_video is not None or pending_audio is not None:
        if timestamp(pending_video) <= timestamp(pending_audio):
            yield pending_video, video_stream
            pending_video = advance(video_packets)
        else:
            yield pending_audio, audio_stream
            pending_audio = advance(audio_packets)


def mux_tracks(
    video_path: str | Path,
    audio_path: str | Path | None,
    target_path: str | Path,
    container: ContainerChoice,
    callback: Callable[[int, int], None] | None = None,
) -> Path:
    """Write both tracks into one container at `target_path`.

    Writes to a temporary file beside the target and moves it into place only
    once the containers are closed, so an existing finished file is never
    replaced by a half-written one - and a mux that fails leaves the target
    exactly as it was.

    The inputs are left alone on every path, success or failure. They are the
    expensive part: a failed mux costs a retry, not another download.
    """
    try:
        from av import open as av_open
    except (ModuleNotFoundError, ImportError) as error:
        raise MuxError(
            f"PyAV is required to combine video and audio: {error}"
        ) from error

    target = Path(target_path)
    temporary = target.with_name(target.name + ".muxing.tmp")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.unlink(missing_ok=True)

    total_bytes = os.path.getsize(video_path) + (
        os.path.getsize(audio_path) if audio_path is not None else 0
    )
    written = 0

    video_input = audio_input = output = None
    try:
        video_input = av_open(str(video_path))
        audio_input = av_open(str(audio_path)) if audio_path is not None else None
        options = {"movflags": "faststart"} if container.format_name == "mp4" else {}
        output = av_open(
            str(temporary), mode="w", format=container.format_name, options=options
        )

        video_stream = video_input.streams.video[0]
        out_video = output.add_stream_from_template(template=video_stream)
        audio_stream = out_audio = None
        if audio_input is not None:
            audio_stream = next(
                (stream for stream in audio_input.streams if stream.type == "audio"), None
            )
            if audio_stream is None:
                raise MuxError(f"{audio_path} carries no audio stream")
            out_audio = output.add_stream_from_template(template=audio_stream)
            _warn_on_drift(
                _stream_duration(video_stream), _stream_duration(audio_stream)
            )

        if audio_stream is None:
            packets = ((packet, video_stream) for packet in video_input.demux(video_stream)
                       if packet.dts is not None)
        else:
            packets = _interleaved(video_input, audio_input, video_stream, audio_stream)

        for packet, origin in packets:
            packet.stream = out_video if origin is video_stream else out_audio
            output.mux(packet)
            written += getattr(packet, "size", 0) or 0
            if callback is not None:
                callback(min(written, total_bytes), total_bytes)
    except MuxError:
        raise
    except Exception as error:  # noqa: BLE001 - every PyAV failure is one MuxError
        raise MuxError(
            f"Could not combine the tracks into {container.format_name}: {error}"
        ) from error
    finally:
        # Writer first, then the readers: relying on garbage collection here is
        # what makes the move below fail on Windows.
        _close_quietly(output, "output")
        _close_quietly(audio_input, "audio input")
        _close_quietly(video_input, "video input")

    try:
        _replace_with_retry(str(temporary), str(target))
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise MuxError(f"Could not move the muxed file into place: {error}") from error

    if callback is not None:
        callback(total_bytes, total_bytes)
    logger.info(
        "Muxed %s into %s (%s bytes in)", container.format_name, target.name, total_bytes
    )
    return target
