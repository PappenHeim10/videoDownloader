"""YouTube provider adapter.

The extraction strategy here was measured, not assumed, and the measurement
overturned the obvious design. Tracing a cold, logged-out client on 2026-09-05
and 2026-09-06:

* **The web page publishes almost nothing usable.** `ytInitialPlayerResponse`
  carries one progressive format (360p, itag 18) and thirty adaptive ones. All
  thirty have *neither* a media URL *nor* a `signatureCipher` - the web player is
  served over SABR/UMP instead - and the one URL that is published answers HTTP
  403 from any cold client, under every header combination tried.

* **Which Innertube client you ask as decides everything.** Of eight contexts
  probed, six were closed or degraded to a cold client. `IOS` answered with
  fetchable URLs, but `googlevideo` served only a bounded *prefix* of each file -
  0.7 % of one ten-hour video - and no request form got past it. `yt-dlp` asks
  as `VISIONOS`, whose URLs carry an otherwise identical parameter set and have
  no such boundary. That difference is not something a request can control; it
  is which client minted the URL.

So this adapter does not parse the watch page, does not touch the player
JavaScript, and does not pick a client. `yt-dlp` does all three, and keeps doing
them across roughly 175 releases a year. What stays here is everything that is
ours: which URLs we claim, how a format becomes a `MediaSource`, and what each
failure is called.

Two things this file must never do, both learned from measurement:

* **Never pass `verbose`, and never log a format dict.** yt-dlp's debug path
  prints the full signed URL - expiry, viewer IP, session id and signature - and
  `format["url"]` is over a thousand characters of the same. A redacting logger
  is injected instead.
* **Never use a `<video>+<audio>` format selector.** Without ffmpeg on PATH
  yt-dlp aborts the merge *and discards what it already downloaded*. Tracks are
  fetched one selector at a time and combined by `application.muxing`.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from base_api.models import Media, MediaSource, MediaTrackInfo
from base_api.modules.errors import UnsupportedURLError

from video_downloader.application.track_download import YTDLP_TRANSPORT
# Shared with the X adapter and with the download layer, which reaches yt-dlp on
# the same terms. Re-exported so callers keep importing it from here.
from video_downloader.providers.ytdlp_options import (  # noqa: F401
    _RedactingLogger,
    base_options,
)

logger = logging.getLogger(__name__)

#: Hosts whose watch URLs this adapter claims.
_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
    "youtu.be", "www.youtu.be",
})

#: Path prefixes that name a single video.
_VIDEO_PREFIXES = ("shorts", "embed", "v", "live")

#: Path prefixes that name something this adapter deliberately does not handle.
#: Claimed anyway, so the refusal can say *why* rather than "unsupported URL".
_COLLECTION_PREFIXES = ("playlist", "channel", "c", "user", "feed", "results", "hashtag")

#: Exactly eleven characters of YouTube's own alphabet. Matched whole, so a
#: path segment that merely looks id-shaped cannot be mistaken for one.
_VIDEO_ID = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")

#: "1080p", "1080p60", "2160p60" - what yt-dlp puts in `format_note` for a video
#: track. The number is YouTube's own tier, which is the only trustworthy one: a
#: portrait Short is 1080x1920 and labelled "1080p", and one YouTube rendition is
#: 608x1080 and labelled "480p", so neither dimension can be used to derive it.
_QUALITY_LABEL = re.compile(r"\A(\d{2,5})p(\d{2,3})?\Z")

#: Protocols this application can hand to a downloader. Storyboards (`mhtml`)
#: and manifest-only entries are not media files.
_FETCHABLE_PROTOCOLS = frozenset({"https", "http"})

#: yt-dlp's marker for the original / provider-default audio track.
_DEFAULT_LANGUAGE_PREFERENCE = 10


class YouTubeError(Exception):
    """Base class for the adapter's own failures."""


class YouTubeUnsupportedTargetError(YouTubeError):
    """A YouTube URL that names something other than one video.

    Its own type because "playlists are not supported" and "this link is not
    ours" are different sentences, and a user who pasted a playlist deserves the
    first one.
    """


class YouTubeExtractionError(YouTubeError):
    """The video could not be resolved for a technical reason.

    Covers transport failures, an unreadable answer, and a video that does not
    exist. Always raised `from` the original exception so the cause survives.
    """


class YouTubeUnavailableError(YouTubeError):
    """YouTube states this video may not be played without more than we have.

    Private, members-only, age-restricted, region-blocked, or behind a bot
    check. Deliberately not an extraction error: nothing failed, and a retry
    returns the same answer. This application does not attempt to get past any
    of them.
    """


class YouTubeLiveNotSupportedError(YouTubeError):
    """A livestream or a premiere, which this application does not download."""


class YouTubeNoSupportedSourceError(YouTubeExtractionError):
    """The answer was readable, but nothing in it can be fetched."""


class YouTubePlayerContractError(YouTubeError):
    """The resolver could not produce a usable URL for a reason it names.

    Its own type because there is exactly one honest response: tell the user to
    update. A wrong media URL is indistinguishable from a working one until it
    answers 403, so this must surface at extraction time and never as a silently
    empty format list.
    """


def _canonical_video_id(url: str) -> Optional[str]:
    """The eleven-character video id iff `url` names one video, else `None`.

    Pure parsing: no network, no side effects, the same answer every time.
    Raises `YouTubeUnsupportedTargetError` for a YouTube URL that names a
    playlist, channel or feed - those are ours to refuse with a reason, not to
    silently disown.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host not in _HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]

    if host in ("youtu.be", "www.youtu.be"):
        # The share form: the id is the whole path, and `?t=30` is a start time.
        candidate = segments[0] if len(segments) == 1 else None
        return candidate if candidate and _VIDEO_ID.match(candidate) else None

    if not segments:
        return None

    if segments[0] == "watch":
        # `v` may legitimately sit beside `list` and `index`; only `v` is read.
        values = parse_qs(parts.query).get("v") or []
        candidate = values[0] if values else None
        return candidate if candidate and _VIDEO_ID.match(candidate) else None

    if segments[0] in _COLLECTION_PREFIXES or segments[0].startswith("@"):
        raise YouTubeUnsupportedTargetError(
            "Playlists und Kanäle werden nicht unterstützt - bitte einen "
            "einzelnen Video-Link angeben."
        )

    if len(segments) == 2 and segments[0] in _VIDEO_PREFIXES:
        return segments[1] if _VIDEO_ID.match(segments[1]) else None

    return None


def _stated_int(value: Any) -> Optional[int]:
    """The integer the resolver stated, or `None` when it stated none."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _codec(value: Any) -> Optional[str]:
    """A codec string, or `None` for yt-dlp's "there is no such track"."""
    if not isinstance(value, str) or not value or value == "none":
        return None
    return value


def _bitrate_bps(entry: dict) -> Optional[int]:
    """Bits per second. yt-dlp states kbit/s as a float; the model wants bits."""
    for key in ("vbr", "abr", "tbr"):
        value = entry.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value * 1000)
    return None


def _quality(entry: dict) -> tuple[Optional[int], Optional[str]]:
    """YouTube's own tier and label for a video format.

    The label is the only trustworthy source for the tier. A portrait Short is
    1080x1920 and labelled "1080p"; one rendition measured 608x1080 and is
    labelled "480p". Neither dimension yields the number YouTube uses, and a
    caller ranking by height puts a portrait 1080p above a genuine 1440p.

    Where YouTube publishes no label at all - it does, for some newer itags -
    the shorter side is the documented fallback rather than a silent guess.
    """
    note = entry.get("format_note")
    if isinstance(note, str):
        match = _QUALITY_LABEL.match(note.strip())
        if match:
            return int(match.group(1)), note.strip()

    width, height = _stated_int(entry.get("width")), _stated_int(entry.get("height"))
    if width and height:
        return min(width, height), None
    return None, None


def _is_default_audio(entry: dict, multi_language: bool) -> Optional[bool]:
    """Whether this is the original track, when the video has more than one.

    Only meaningful where YouTube publishes dubbed renditions: then exactly the
    original carries `language_preference == 10`. On a video with one audio
    track it says nothing, and `None` is the honest answer - the model
    distinguishes "not the default" from "no such concept here".
    """
    if not multi_language:
        return None
    return entry.get("language_preference") == _DEFAULT_LANGUAGE_PREFERENCE


def _is_drc(entry: dict, any_drc: bool) -> Optional[bool]:
    """Whether this is a loudness-normalised copy of another track.

    yt-dlp marks it twice - a `-drc` suffix on the format id and "DRC" in the
    note - and both are read, because either could be the one a future release
    keeps. Where the video publishes no DRC rendition at all the flag says
    nothing about the tracks it does publish.
    """
    if not any_drc:
        return None
    format_id = str(entry.get("format_id") or "")
    note = str(entry.get("format_note") or "")
    return format_id.endswith("-drc") or "DRC" in note


def _role(video_codec: Optional[str], audio_codec: Optional[str]) -> Optional[str]:
    if video_codec and audio_codec:
        return "combined"
    if video_codec:
        return "video"
    if audio_codec:
        return "audio"
    return None


def _fetchable(entry: Any) -> bool:
    """Whether this application can hand the entry to a downloader as one file."""
    if not isinstance(entry, dict) or not entry.get("url"):
        return False
    if entry.get("protocol") not in _FETCHABLE_PROTOCOLS:
        return False
    return bool(_codec(entry.get("vcodec")) or _codec(entry.get("acodec")))


class YouTubeAdapter:
    """Resolves a YouTube watch URL into a provider-neutral `Media`.

    Holds no client and no session: `yt_dlp.YoutubeDL` is constructed per call
    and closed with it, so nothing accumulated during one resolution can reach
    the next, and an adapter the registry builds for a `supports()`-only job
    costs nothing.
    """

    def __init__(self, resolver: Any = None) -> None:
        # Injectable so the tests can drive stored, redacted fixtures without a
        # network and without patching a module global.
        self._resolver = resolver

    def supports(self, url: str) -> bool:
        """Whether this adapter claims `url`. Cheap, synchronous, network-free."""
        try:
            return _canonical_video_id(url) is not None
        except YouTubeUnsupportedTargetError:
            # A playlist URL is ours - claiming it is what lets `resolve` say
            # why it is refused instead of the registry saying "unsupported".
            return True

    async def resolve(self, url: str) -> Media:
        """Resolve a watch URL into `Media` with every fetchable track."""
        video_id = _canonical_video_id(url)
        if video_id is None:
            raise UnsupportedURLError(f"Not a supported YouTube video URL: {url}")

        info = self._extract(url)
        if not isinstance(info, dict):
            # Checked here rather than in `_extract`, so it holds for an
            # injected resolver too: nothing below may assume a shape.
            raise YouTubeExtractionError(
                f"The resolver returned {type(info).__name__}, not a video description"
            )

        self._refuse_unplayable(info)

        formats = [entry for entry in (info.get("formats") or []) if _fetchable(entry)]
        if not formats:
            raise YouTubeNoSupportedSourceError(
                f"YouTube offers no fetchable format for {video_id}"
            )

        sources = self._sources_from(formats, video_id)
        if not sources:
            raise YouTubeNoSupportedSourceError(
                f"None of YouTube's {len(formats)} formats for {video_id} is usable"
            )

        title = info.get("title")
        return Media(
            provider="youtube",
            original_url=url,
            title=title if isinstance(title, str) and title.strip() else video_id,
            provider_id=video_id,
            authors=[info["uploader"]] if isinstance(info.get("uploader"), str) else [],
            thumbnail=info.get("thumbnail") if isinstance(info.get("thumbnail"), str) else None,
            duration=_stated_int(info.get("duration")),
            sources=sources,
        )

    def _extract(self, url: str) -> dict:
        """One resolution, with every failure mapped onto this adapter's names."""
        if self._resolver is not None:
            return self._resolver(url)

        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError

        try:
            with YoutubeDL(base_options(skip_download=True)) as resolver:
                info = resolver.extract_info(url, download=False)
        except GeoRestrictedError as error:
            raise YouTubeUnavailableError("In dieser Region nicht verfügbar.") from error
        except (DownloadError, ExtractorError) as error:
            raise self._classify(str(error), error) from error
        except OSError as error:
            raise YouTubeExtractionError(f"Request to YouTube failed: {error}") from error

        return info

    @staticmethod
    def _classify(message: str, error: Exception) -> YouTubeError:
        """Turn one resolver message into the failure this application names.

        Matched on the text because that is what the resolver gives us: it
        reports YouTube's own `playabilityStatus.reason` verbatim, and those
        strings are the contract we actually have. Anything unrecognised stays
        an extraction error rather than being guessed into a friendlier one.
        """
        lowered = message.lower()
        if "sign in to confirm you" in lowered and "bot" in lowered:
            return YouTubeUnavailableError(
                "YouTube verlangt eine Anmeldung. Wird nicht unterstützt."
            )
        if "private video" in lowered or "this video is private" in lowered:
            return YouTubeUnavailableError("Video ist privat.")
        if "age" in lowered and ("confirm your age" in lowered or "age-restricted" in lowered):
            return YouTubeUnavailableError(
                "Altersbeschränktes Video - nicht unterstützt."
            )
        if "members-only" in lowered or "join this channel" in lowered:
            return YouTubeUnavailableError("Nur für Kanalmitglieder verfügbar.")
        if "not available in your country" in lowered or "geo" in lowered and "block" in lowered:
            return YouTubeUnavailableError("In dieser Region nicht verfügbar.")
        if "live event will begin" in lowered or "premieres in" in lowered:
            return YouTubeLiveNotSupportedError(
                "Livestreams und Premieren werden nicht unterstützt."
            )
        if "javascript" in lowered and "runtime" in lowered:
            return YouTubePlayerContractError(
                "YouTube verlangt eine neuere Extraktion; bitte die Anwendung aktualisieren."
            )
        if "nsig" in lowered or "signature" in lowered or "player" in lowered and "extract" in lowered:
            return YouTubePlayerContractError(
                "YouTube verlangt eine neuere Extraktion; bitte die Anwendung aktualisieren."
            )
        if "video unavailable" in lowered or "does not exist" in lowered or "removed" in lowered:
            return YouTubeExtractionError("Video nicht gefunden oder entfernt.")
        return YouTubeExtractionError(f"YouTube could not be resolved: {error}")

    @staticmethod
    def _refuse_unplayable(info: dict) -> None:
        """Live and premiere are refused before any format is looked at.

        Detectable without a media request: a livestream states `is_live`, and
        every one measured states no size for any format - which would make the
        transport's completeness check meaningless if it got that far.
        """
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            raise YouTubeLiveNotSupportedError(
                "Livestreams werden nicht unterstützt."
            )

    @classmethod
    def _sources_from(cls, formats: list[dict], video_id: str) -> list[MediaSource]:
        """Translate every fetchable format into one `MediaSource`.

        The two "is this flag meaningful here?" questions are answered over the
        whole format list rather than per entry, because that is the only level
        at which they *are* answerable: a video with one audio track says
        nothing about default tracks, and one with no DRC rendition says nothing
        about dynamic range compression.
        """
        audio_entries = [
            entry for entry in formats
            if _codec(entry.get("acodec")) and not _codec(entry.get("vcodec"))
        ]
        multi_language = any(
            entry.get("language_preference") == _DEFAULT_LANGUAGE_PREFERENCE
            for entry in audio_entries
        )
        any_drc = any(
            str(entry.get("format_id") or "").endswith("-drc")
            or "DRC" in str(entry.get("format_note") or "")
            for entry in audio_entries
        )

        sources = []
        for entry in formats:
            source = cls._source_from_format(entry, video_id, multi_language, any_drc)
            if source is not None:
                sources.append(source)
        return sources

    @staticmethod
    def _source_from_format(
        entry: dict, video_id: str, multi_language: bool, any_drc: bool
    ) -> Optional[MediaSource]:
        video_codec = _codec(entry.get("vcodec"))
        audio_codec = _codec(entry.get("acodec"))
        role = _role(video_codec, audio_codec)
        if role is None:
            return None

        tier, label = _quality(entry) if video_codec else (None, None)
        headers = entry.get("http_headers")
        format_id = str(entry.get("format_id") or "")

        return MediaSource(
            url=entry["url"],
            # Fetched by the resolver that produced the URL rather than by the
            # engine's transport. `source_type` names the transport, which is
            # exactly the question it has always answered.
            source_type=YTDLP_TRANSPORT,
            headers=dict(headers) if isinstance(headers, dict) else {},
            expected_size=_stated_int(entry.get("filesize"))
            or _stated_int(entry.get("filesize_approx")),
            quality_value=tier,
            quality_label=label,
            # Stable across a re-resolution, which the URL is not: it expires
            # within hours and comes back different for byte-identical content.
            identity=f"youtube:{video_id}:{format_id}" if format_id else None,
            track=MediaTrackInfo(
                role=role,
                container=entry.get("ext") if isinstance(entry.get("ext"), str) else None,
                # Verbatim, exactly as the resolver reported them. Mapping these
                # to families is the selection layer's decision, not ours.
                video_codec=video_codec,
                audio_codec=audio_codec,
                fps=float(entry["fps"]) if isinstance(entry.get("fps"), (int, float)) else None,
                bitrate_bps=_bitrate_bps(entry),
                width=_stated_int(entry.get("width")),
                height=_stated_int(entry.get("height")),
                language=entry.get("language") if isinstance(entry.get("language"), str) else None,
                is_default_audio=_is_default_audio(entry, multi_language) if audio_codec and not video_codec else None,
                is_dynamic_range_compressed=_is_drc(entry, any_drc) if audio_codec and not video_codec else None,
                # YouTube states a duration for the video, never for a track.
                # The honest per-track measurement comes from the containers.
                duration_ms=None,
            ),
        )
