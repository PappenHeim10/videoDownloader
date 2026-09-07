"""X (formerly Twitter) provider adapter.

Much smaller than the YouTube one, and a measurement explains why. Resolving a
post on 2026-09-07 returns two kinds of format:

* `http-<bitrate>` - a progressive MP4 over plain https, one file carrying both
  picture and sound, published in four sizes. These are what this adapter
  offers, and there is nothing to pair or mux: X hands out finished files.
* `hls-*` - the same video as a segmented playlist, split into video-only
  renditions and separate audio-only ones. Filtered out rather than paired,
  because the progressive file is the same content without the assembly.

Three things the progressive formats do *not* state, all confirmed against the
live site, and each answered here by leaving a field unset rather than filling
it with a plausible value:

* **No codecs.** `vcodec` and `acodec` come back unset - yt-dlp has not opened
  the file and X does not say. `role` therefore stays unset too, which the
  selection layer already reads as "one finished file", and which is exactly
  what these are.
* **No quality label.** `format_note` is empty on every one of them, so the
  tier comes from the short side and `quality_label` stays `None` rather than a
  synthesised "1080p".
* **No size at all.** X states no `filesize`, and the `filesize_approx` yt-dlp
  derives from the bitrate is not a substitute: measured against the real
  `Content-Range` totals on 2026-09-07 it overstated all four formats of one
  post, by 2.52x, 2.69x, 2.81x and 5.70x. So `expected_size` stays unset, which
  costs a progress total the download then learns for itself, and avoids a
  large-download question asked about a number that is wrong by a factor of six.

Resolution goes through the same yt-dlp the YouTube adapter uses, on the same
terms - no cookies, no verbose, a redacting logger - because every reason for
those is about the resolver rather than about the site.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from base_api.models import Media, MediaSource, MediaTrackInfo
from base_api.modules.errors import UnsupportedURLError

from video_downloader.application.track_download import YTDLP_TRANSPORT
from video_downloader.providers.ytdlp_options import base_options

logger = logging.getLogger(__name__)

#: Hosts this adapter claims. Both names are live - x.com is current and
#: twitter.com still redirects - so links of either age turn up in a paste box.
_HOSTS = frozenset({
    "x.com", "www.x.com", "mobile.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com",
})

#: A post id is a snowflake: digits only. Matched by shape rather than by an
#: exact length, because the length grows with time and pinning today's would
#: quietly stop claiming valid links some years from now.
_POST_ID = re.compile(r"\A[0-9]{6,25}\Z")

#: What X appends when one attachment of a post is opened directly;
#: `/status/<id>/video/1` is the same post as `/status/<id>`.
_ATTACHMENT_SEGMENTS = ("video", "photo")

#: First path segments that name a feed rather than an account. Claimed anyway,
#: so the refusal can say what the link is instead of "unsupported URL".
_COLLECTION_PREFIXES = (
    "home", "explore", "search", "notifications", "messages", "settings",
    "compose", "hashtag", "bookmarks", "lists", "topics",
)

#: The status forms under X's internal `/i/` namespace.
_INTERNAL_POST_PATHS = (("i", "status"), ("i", "web", "status"))

#: Protocols this application can hand to a downloader as one file. The `hls-*`
#: entries arrive as `m3u8_native` and are excluded by this.
_FETCHABLE_PROTOCOLS = frozenset({"https", "http"})

#: X wraps every link in a post as a t.co shortener, and yt-dlp composes the
#: title out of the post text, so a title very often ends in one. It has to go
#: before the title becomes a filename: the sanitiser keeps only the last path
#: component of what it is handed, so "Poster - https://t.co/abc" arrives on
#: disk as "abc.mp4" - a file named after a shortener token nobody can read.
_SHORTENED_LINK = re.compile(r"https?://t\.co/\S*")


class XError(Exception):
    """Base class for the adapter's own failures."""


class XUnsupportedTargetError(XError):
    """An X URL that names something other than one post.

    Its own type because "profiles are not supported" and "this link is not
    ours" are different sentences, and someone who pasted a profile deserves
    the first one.
    """


class XExtractionError(XError):
    """The post could not be resolved for a technical reason.

    Covers transport failures, an unreadable answer, and a post that no longer
    exists. Always raised `from` the original exception.
    """


class XUnavailableError(XError):
    """X states this post may not be read without more than we have.

    A protected account, an age-restricted post, a suspended account, or a rate
    limit. Deliberately not an extraction error: nothing failed, and a retry
    returns the same answer. This application does not attempt to get past any
    of them.
    """


class XLiveNotSupportedError(XError):
    """A live broadcast or a Space, which this application does not download."""


class XNoSupportedSourceError(XExtractionError):
    """The post was read, and carries no video.

    The ordinary case by far - a post with only text, a photo or a link
    preview - so it gets its own type and its own sentence rather than being
    reported as something having gone wrong.
    """


def _canonical_post_id(url: str) -> Optional[str]:
    """The numeric post id iff `url` names one post, else `None`.

    Pure parsing: no network, no side effects, the same answer every time.
    Raises `XUnsupportedTargetError` for an X URL that names a profile or a
    feed - those are ours to refuse with a reason, not to silently disown.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").lower() not in _HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    lowered = [segment.lower() for segment in segments]

    if not segments:
        raise XUnsupportedTargetError(
            "Das ist die Startseite von X, kein einzelner Beitrag - bitte den "
            "Link zu einem Beitrag angeben."
        )

    if lowered[0] == "i":
        for prefix in _INTERNAL_POST_PATHS:
            if tuple(lowered[:len(prefix)]) == prefix and len(segments) > len(prefix):
                return _post_id(segments[len(prefix)])
        raise XUnsupportedTargetError(
            "Dieser X-Link zeigt auf keinen einzelnen Beitrag."
        )

    if lowered[0] in _COLLECTION_PREFIXES:
        raise XUnsupportedTargetError(
            "Feeds, Suchergebnisse und Listen werden nicht unterstuetzt - bitte "
            "den Link zu einem einzelnen Beitrag angeben."
        )

    # `/<user>/status/<id>`, the older `/<user>/statuses/<id>`, and either with
    # the `/video/1` suffix X adds when an attachment is opened directly.
    if len(segments) >= 3 and lowered[1] in ("status", "statuses"):
        if len(segments) > 3 and lowered[3] not in _ATTACHMENT_SEGMENTS:
            return None
        return _post_id(segments[2])

    if len(segments) <= 2:
        # `/<user>`, and `/<user>/likes`, `/<user>/with_replies` and friends.
        raise XUnsupportedTargetError(
            "Das ist ein Profil, kein einzelner Beitrag - bitte den Link zu "
            "einem Beitrag angeben."
        )

    return None


def _post_id(candidate: str) -> Optional[str]:
    return candidate if _POST_ID.match(candidate) else None


def _stated_int(value: Any) -> Optional[int]:
    """The integer the resolver stated, or `None` when it stated none."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _codec(value: Any) -> Optional[str]:
    """A codec string, or `None` for "the resolver named none".

    X names none on every progressive format, so this returns `None` in
    practice. It stays because the alternative - deriving a codec from the
    `.mp4` extension - would be this adapter stating a fact X did not.
    """
    if not isinstance(value, str) or not value or value == "none":
        return None
    return value


def _fetchable(entry: Any) -> bool:
    """Whether this entry is one file this application can hand to a downloader.

    Dimensions are the test rather than codecs: X states no codecs at all on the
    formats that *are* fetchable, and every one of them states a width and a
    height. The audio-only renditions have neither, and are excluded by the
    protocol check before this even matters.
    """
    if not isinstance(entry, dict) or not entry.get("url"):
        return False
    if entry.get("protocol") not in _FETCHABLE_PROTOCOLS:
        return False
    return bool(_stated_int(entry.get("width")) and _stated_int(entry.get("height")))


class XAdapter:
    """Resolves an X post URL into a provider-neutral `Media`.

    Holds no client and no session: `yt_dlp.YoutubeDL` is constructed per call
    and closed with it, so nothing accumulated during one resolution can reach
    the next.
    """

    def __init__(self, resolver: Any = None) -> None:
        # Injectable so the tests can drive stored, redacted fixtures without a
        # network and without patching a module global.
        self._resolver = resolver

    def supports(self, url: str) -> bool:
        """Whether this adapter claims `url`. Cheap, synchronous, network-free."""
        try:
            return _canonical_post_id(url) is not None
        except XUnsupportedTargetError:
            # A profile URL is ours - claiming it is what lets `resolve` say why
            # it is refused instead of the registry saying "unsupported".
            return True

    async def resolve(self, url: str) -> Media:
        """Resolve a post URL into `Media` with every fetchable file."""
        post_id = _canonical_post_id(url)
        if post_id is None:
            raise UnsupportedURLError(f"Not a supported X post URL: {url}")

        info = self._extract(url)
        if not isinstance(info, dict):
            # Checked here rather than in `_extract`, so it holds for an
            # injected resolver too: nothing below may assume a shape.
            raise XExtractionError(
                f"The resolver returned {type(info).__name__}, not a post description"
            )

        self._refuse_unplayable(info)

        formats = [entry for entry in (info.get("formats") or []) if _fetchable(entry)]
        if not formats:
            raise XNoSupportedSourceError("Dieser Beitrag enthaelt kein Video.")

        sources = [
            source
            for source in (self._source_from_format(e, post_id) for e in formats)
            if source is not None
        ]
        if not sources:
            raise XNoSupportedSourceError(
                f"None of the {len(formats)} formats X offers for {post_id} is usable"
            )

        return Media(
            provider="x",
            original_url=url,
            # X posts have no title. yt-dlp composes one from the poster's name
            # and the post text; it is kept as given, because deciding what a
            # post is "called" is not this adapter's judgement to make.
            title=self._title(info, post_id),
            provider_id=post_id,
            authors=[info["uploader"]] if isinstance(info.get("uploader"), str) else [],
            thumbnail=info.get("thumbnail") if isinstance(info.get("thumbnail"), str) else None,
            # Stated in seconds and fractional - 12.422 for the post measured.
            duration=_stated_int(info.get("duration")),
            sources=sources,
        )

    @staticmethod
    def _title(info: dict, post_id: str) -> str:
        """What to call a post, which X itself does not name.

        yt-dlp composes "<poster> - <post text>", and the post text is often
        nothing but a shortened link. Those are stripped, and what remains is
        used only if it says more than the poster's name already does -
        otherwise the post id joins it, so two posts by one account do not
        arrive on disk under the same name.
        """
        uploader = info.get("uploader")
        uploader = uploader.strip() if isinstance(uploader, str) else ""

        title = info.get("title")
        if isinstance(title, str):
            spelled_out = _SHORTENED_LINK.sub("", title)
            spelled_out = re.sub(r"\s+", " ", spelled_out).strip(" -–—")
            if spelled_out and spelled_out != uploader:
                return spelled_out

        return f"{uploader} - {post_id}" if uploader else post_id

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
            raise XUnavailableError("In dieser Region nicht verfuegbar.") from error
        except (DownloadError, ExtractorError) as error:
            raise self._classify(str(error), error) from error
        except OSError as error:
            raise XExtractionError(f"Request to X failed: {error}") from error

        return info

    @staticmethod
    def _classify(message: str, error: Exception) -> XError:
        """Turn one resolver message into the failure this application names.

        Matched on the text because that is what the resolver gives us: it
        reports X's own wording, and those strings are the contract we actually
        have. Anything unrecognised stays an extraction error rather than being
        guessed into a friendlier one.
        """
        lowered = message.lower()
        if "no video could be found" in lowered or "no media" in lowered:
            return XNoSupportedSourceError("Dieser Beitrag enthaelt kein Video.")
        if "nsfw" in lowered or ("age" in lowered and "restrict" in lowered):
            return XUnavailableError("Altersbeschraenkter Beitrag - nicht unterstuetzt.")
        if "protected" in lowered or "private" in lowered:
            return XUnavailableError("Dieses Konto ist geschuetzt.")
        if "suspended" in lowered:
            return XUnavailableError("Dieses Konto ist gesperrt.")
        if "rate limit" in lowered or "too many requests" in lowered:
            return XUnavailableError(
                "X hat die Anfrage vorerst abgelehnt - bitte spaeter erneut versuchen."
            )
        if "log in" in lowered or "login" in lowered or "authenticat" in lowered:
            return XUnavailableError("X verlangt eine Anmeldung. Wird nicht unterstuetzt.")
        if "not found" in lowered or "unavailable" in lowered or "deleted" in lowered:
            return XExtractionError("Beitrag nicht gefunden oder geloescht.")
        return XExtractionError(f"X could not be resolved: {error}")

    @staticmethod
    def _refuse_unplayable(info: dict) -> None:
        """Live broadcasts and Spaces are refused before any format is read."""
        if info.get("is_live") or info.get("live_status") in (
            "is_live", "is_upcoming", "post_live"
        ):
            raise XLiveNotSupportedError(
                "Live-Uebertragungen und Spaces werden nicht unterstuetzt."
            )

    @staticmethod
    def _source_from_format(entry: dict, post_id: str) -> Optional[MediaSource]:
        width = _stated_int(entry.get("width"))
        height = _stated_int(entry.get("height"))
        if not width or not height:
            return None

        format_id = str(entry.get("format_id") or "")
        bitrate = entry.get("tbr")
        headers = entry.get("http_headers")

        return MediaSource(
            url=entry["url"],
            # Fetched by the resolver that produced the URL rather than by the
            # engine's transport. The download layer probes each source once and
            # moves it onto the engine when it can read the whole file - which
            # for X it can: these are ordinary CDN objects.
            source_type=YTDLP_TRANSPORT,
            headers=dict(headers) if isinstance(headers, dict) else {},
            # Only a size X actually stated, which for these formats means
            # none. `filesize_approx` is bitrate times duration, and measured
            # against the real totals it was 2.5x to 5.7x too high - enough to
            # ask about a two-gigabyte download that is four hundred megabytes,
            # and enough to make the readability probe ask for a byte past the
            # end of the file and conclude the file is unreadable.
            expected_size=_stated_int(entry.get("filesize")),
            # The short side. X publishes no label of its own, and this is the
            # only tier that survives the portrait videos X is full of.
            quality_value=min(width, height),
            quality_label=None,
            # Stable across a re-resolution, which the URL is not.
            identity=f"x:{post_id}:{format_id}" if format_id else None,
            track=MediaTrackInfo(
                # Left unset deliberately: X states no codecs, so nothing here
                # knows whether the file carries sound. A source that states no
                # role is read as one finished file, which is what it is.
                role=None,
                container=entry.get("ext") if isinstance(entry.get("ext"), str) else None,
                video_codec=_codec(entry.get("vcodec")),
                audio_codec=_codec(entry.get("acodec")),
                bitrate_bps=int(bitrate * 1000)
                if isinstance(bitrate, (int, float)) and bitrate > 0
                else None,
                width=width,
                height=height,
            ),
        )
