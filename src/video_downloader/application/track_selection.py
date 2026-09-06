"""Choosing what one job downloads, from a `Media` alone.

Nothing here opens a connection, so a provider that resolved offline stays
downloadable offline and the choice is reproducible from the `Media`. Nothing
here knows a provider name either: it reads `MediaSource` and
`MediaSource.track`, both of which the engine defines and every adapter fills.

Three concerns live here, deliberately kept apart because merging them is how
selection rules become unexplainable:

* **what the user asked for** - "best", "1080p", 720. A position in a ranked
  list, a provider's own label, or a numeric tier, and never one silently
  reinterpreted as another.
* **what the file is** - codec and container, which decide what can be muxed
  losslessly into what.
* **how tracks couple** - which audio belongs with the chosen video, and which
  renditions are not candidates at all.

The codec taxonomy lives here rather than in the engine. `MediaTrackInfo` keeps
codec strings verbatim on purpose; deciding that `avc1.640028` and `avc1.4d401f`
are the same thing for the purpose of picking a container is policy, and policy
belongs to whoever is choosing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from base_api.models import MediaSource
from base_api.modules.errors import UnsupportedProtocolError

logger = logging.getLogger(__name__)

#: The three orderings that name a position in the list rather than a tier.
QUALITY_PREFERENCES = frozenset({"best", "worst", "half"})

#: "1080", "1080p", "1080P" - a number with an optional trailing p, nothing else.
_NUMERIC_QUALITY = re.compile(r"\A(\d+)[pP]?\Z")

#: Codec families this application knows how to reason about, keyed by the
#: prefix a provider's verbatim codec string starts with. Anything unlisted maps
#: to `None`, which means "we have no opinion" and never "assume the common
#: case" - a guessed family would pick a container the file cannot go into.
_VIDEO_FAMILIES = ("avc1", "avc3", "vp09", "vp9", "av01", "hev1", "hvc1")
_AUDIO_FAMILIES = ("mp4a", "opus", "vorbis", "ec-3", "ac-3", "flac")

#: Video codecs in the order this application prefers them, most preferred
#: first. Compatibility before size: H.264 plays everywhere, and the files that
#: are twice as large are the ones a user can actually open.
VIDEO_CODEC_PREFERENCE = ("avc1", "vp09", "av01")

#: Above this tier there is no H.264 at all on the providers measured, so the
#: preference above cannot be satisfied and the VP9 branch takes over.
H264_CEILING = 1080


@dataclass(frozen=True)
class TrackSelection:
    """What one job will download: one finished file, or two tracks to mux.

    Never both, and never neither - `select_tracks` raises rather than return an
    empty selection, because "nothing to download" is a failure the caller has
    to report, not a result it should have to check for.
    """

    combined: MediaSource | None = None
    video: MediaSource | None = None
    audio: MediaSource | None = None

    @property
    def needs_muxing(self) -> bool:
        return self.combined is None

    @property
    def sources(self) -> tuple[MediaSource, ...]:
        """Every source to download, in the order to download them."""
        if self.combined is not None:
            return (self.combined,)
        return tuple(source for source in (self.video, self.audio) if source is not None)


def codec_family(codec: str | None) -> str | None:
    """The family a verbatim codec string belongs to, or `None` if unknown.

    `"avc1.640028"` and `"avc1.4d401f"` are both `"avc1"`: they differ in profile
    and level, which matters to a decoder and not to the question this answers,
    which is what container the track can go into losslessly.

    An unrecognised string is `None` rather than a guess. A wrong family would
    put a track into a container that cannot hold it, and the failure surfaces
    long after the bytes were downloaded.
    """
    if not codec:
        return None
    head = codec.strip().lower().split(".", 1)[0]
    for family in (*_VIDEO_FAMILIES, *_AUDIO_FAMILIES):
        if head == family:
            # vp9 and vp09 are the same family under two spellings; providers
            # use both, sometimes in one response.
            return "vp09" if family == "vp9" else family
    return None


def _numeric_quality(value: str) -> int | None:
    match = _NUMERIC_QUALITY.match(value.strip())
    return int(match.group(1)) if match else None


def _order_key(source: MediaSource) -> tuple[int, int, int, int, str]:
    """Rank one source, worst first.

    The numeric tier is whatever the provider ranks by (`quality_value`), never
    a dimension re-derived from a label, a URL or the pixel height - a portrait
    video's label and its dimensions legitimately disagree, and only the
    provider knows which is which.

    Sources without a numeric tier sort below every source that has one and are
    ordered among themselves by size, so "best" cannot pick an entry whose
    quality nobody stated over a stated 1080p. The remaining components are
    tie-breakers that exist purely so the choice is reproducible: higher frame
    rate first, then the larger file, then the URL.
    """
    numeric = source.quality_value
    fps = source.track.fps
    return (
        0 if numeric is None else 1,
        numeric or 0,
        int(fps) if fps else 0,
        source.expected_size or 0,
        source.url,
    )


def _select_by_number(ordered: list[MediaSource], target: int) -> MediaSource:
    """Exact numeric tier, else the next smaller one, else the smallest."""
    numeric = [source for source in ordered if source.quality_value is not None]
    if not numeric:
        return ordered[0]

    exact = [source for source in numeric if source.quality_value == target]
    if exact:
        # Already ordered, so the last one is the best-ranked of the ties.
        return exact[-1]

    smaller = [source for source in numeric if cast(int, source.quality_value) < target]
    if smaller:
        # Downwards, never upwards: a user asking for 720 on a connection that
        # suits 720 should not silently receive 2160.
        return smaller[-1]
    return numeric[0]


def select_progressive_source(
    sources: Sequence[MediaSource], quality: str | int
) -> MediaSource:
    """Pick one source for `quality`. No requests, no guessing.

    The semantics differ by the *type* of what is asked for, and deliberately
    so - nothing is coerced from one into the other:

    * `"best"` / `"worst"` / `"half"` (case-insensitive) name a position in the
      ranked list and use the numeric tier only.
    * a **string** is first matched against the provider's own quality label,
      compared exactly apart from case and surrounding whitespace. This is what
      makes `"1080p"` find a portrait video the provider labels `"1080p"` and
      ranks as 1920 - the label is the provider's word for it.
    * an **integer** never matches a label. `1080` is a tier, `"1080p"` is a
      name, and the two can point at different files on the same video; making
      the integer fall back to label matching would make that difference depend
      on how a caller happened to spell its argument.

    Both spellings then fall through to the same numeric rule: exact tier, else
    the next smaller tier, else the smallest video available. The distinction is
    about what *matches*, never about what is ultimately returned - with nothing
    smaller to fall back to, the last rule can hand an integer request the very
    file its label would have matched. Two spellings agreeing on a result is not
    evidence that they took the same route to it.

    A string that is neither a preference, nor a label, nor a number cannot be
    interpreted - the best available source is used rather than failing the
    download, because "I do not understand this quality" is not a reason to
    have no video at all.
    """
    ordered = sorted(sources, key=_order_key)
    if not ordered:
        raise UnsupportedProtocolError("No progressive source to choose from.")

    if isinstance(quality, str):
        wanted = quality.strip().casefold()
        if wanted in QUALITY_PREFERENCES:
            if wanted == "worst":
                return ordered[0]
            if wanted == "half":
                return ordered[len(ordered) // 2]
            return ordered[-1]

        labelled = [
            source
            for source in ordered
            if source.quality_label is not None
            and source.quality_label.strip().casefold() == wanted
        ]
        if labelled:
            return labelled[-1]

        target = _numeric_quality(quality)
        if target is None:
            logger.warning(
                "Quality %r matches no label and is not a number; using the best source.",
                quality,
            )
            return ordered[-1]
        return _select_by_number(ordered, target)

    return _select_by_number(ordered, int(quality))


def _is_separate(source: MediaSource, role: str) -> bool:
    """Whether a source explicitly says it carries only video, or only audio.

    A source that says nothing about its role is a finished file, because that
    is what every source has meant since before roles existed and what every
    provider that states no role still means. Pairing is opt-in, never inferred.
    """
    return source.track.role == role


def _preferred_video(candidates: list[MediaSource]) -> MediaSource:
    """Among video tracks of one tier, the one this application wants.

    Compatibility first, and the ceiling is the reason the rule needs two
    branches at all: up to 1080p every provider measured offers H.264, which
    plays on anything. Above it none of them does, so the preference falls
    through to VP9 and the output becomes WebM rather than MP4.

    AV1 is last for now. It is the smallest of the three at equal quality and
    the least widely playable, which makes it a good future user option and a
    poor default.
    """
    by_family: dict[str | None, list[MediaSource]] = {}
    for source in candidates:
        by_family.setdefault(codec_family(source.track.video_codec), []).append(source)

    for family in VIDEO_CODEC_PREFERENCE:
        if family in by_family:
            return by_family[family][-1]

    # No recognised family anywhere - a provider we have no codec opinion about.
    # The ranking already put the best-ranked candidate last.
    return candidates[-1]


def _preferred_audio(
    candidates: list[MediaSource], video: MediaSource | None
) -> MediaSource | None:
    """The audio track that belongs with `video`, or `None` if there is none.

    Three filters, in order, each of which can legitimately empty the list and
    none of which is allowed to:

    * **Dynamic range compression.** A DRC rendition is a loudness-normalised
      copy published beside the original. Nobody asked for it; it is offered.
    * **Default track.** A video with dubbed audio publishes one original track
      and nine machine translations, and only `is_default_audio` tells them
      apart. Where a provider states the flag at all, anything but the default
      is dropped - handing a German user a Hindi dub is worse than any bitrate.
    * **Container match.** Preferring the audio codec that shares a container
      with the chosen video is what keeps the output in MP4 or WebM instead of
      falling back to Matroska.

    Each filter is skipped when it would remove everything, because "no audio at
    all" is a worse answer than "audio that is not ideal".
    """
    if not candidates:
        return None

    usable = [
        source
        for source in candidates
        if source.track.is_dynamic_range_compressed is not True
    ] or candidates

    default_stated = [
        source for source in usable if source.track.is_default_audio is not None
    ]
    if default_stated:
        usable = [
            source for source in default_stated if source.track.is_default_audio
        ] or usable

    video_family = codec_family(video.track.video_codec) if video is not None else None
    wanted = _companion_audio_family(video_family)
    if wanted is not None:
        matching = [
            source for source in usable if codec_family(source.track.audio_codec) == wanted
        ]
        if matching:
            usable = matching

    return max(usable, key=_audio_rank)


def _companion_audio_family(video_family: str | None) -> str | None:
    """The audio codec that shares a container with this video codec."""
    if video_family in ("avc1", "avc3", "av01", "hev1", "hvc1"):
        return "mp4a"
    if video_family == "vp09":
        return "opus"
    return None


def _audio_rank(source: MediaSource) -> tuple[int, str]:
    """Highest stated bitrate wins; the URL keeps ties reproducible."""
    return (source.track.bitrate_bps or source.expected_size or 0, source.url)


def select_tracks(
    sources: Sequence[MediaSource], quality: str | int
) -> TrackSelection:
    """Pick a finished file, or the video and audio tracks to mux into one.

    A combined source always wins when one exists: it needs no muxing, so it
    cannot fail at a step a two-track download has and it produces the container
    the provider already chose. Only when none is offered does the pairing
    happen, and then the video is chosen for the requested quality and the audio
    is chosen to suit the video.

    `"best"` means the best quality available, muxing included. The alternative
    reading - the best that needs no muxing - is not a smaller promise on a
    provider that publishes no combined format at all; it is no promise.
    """
    available = list(sources)
    if not available:
        raise UnsupportedProtocolError("No source to choose from.")

    videos = [source for source in available if _is_separate(source, "video")]
    audios = [source for source in available if _is_separate(source, "audio")]
    combined = [source for source in available if source not in videos and source not in audios]

    if combined:
        return TrackSelection(combined=select_progressive_source(combined, quality))

    if not videos:
        # Audio-only renditions are not a video download. A provider that offers
        # nothing else has offered nothing this application can do.
        raise UnsupportedProtocolError(
            "The provider offers no video track, only audio renditions."
        )

    ordered = sorted(videos, key=_order_key)
    chosen_tier = select_progressive_source(ordered, quality)
    peers = [
        source
        for source in ordered
        if source.quality_value == chosen_tier.quality_value
        and (source.track.fps or 0) == (chosen_tier.track.fps or 0)
    ] or [chosen_tier]

    video = _preferred_video(peers)
    audio = _preferred_audio(audios, video)
    if audio is None:
        # A silent video is a video. Downloading it as one track and muxing
        # nothing is better than refusing it.
        logger.info("No audio track offered; downloading video only.")
        return TrackSelection(combined=video)

    return TrackSelection(video=video, audio=audio)
