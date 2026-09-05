"""PeerTube provider adapter.

The extraction strategy here was measured, not assumed. Tracing the official web
client on video.blender.org (2026-09-05) shows it resolves a watch page by
calling ``GET /api/v1/videos/{id}`` exactly once and taking the HLS master
playlist straight out of ``streamingPlaylists[]``. Two findings from that trace
decide the shape of this file:

* **The HTML carries nothing to extract.** The watch document holds no playlist
  URL - ``window.PeerTubeServerConfig`` is instance configuration, and the
  ``ld+json`` block is SEO metadata. The bootstrap-state extraction the xHamster
  adapter is forced into has no equivalent here, so the REST call is not a
  detour around the normal PeerTube flow; it *is* the request the player makes.
  One page view spends about ten API requests, this adapter spends one.

* **``playlistUrl`` is taken verbatim.** On federated instances it routinely
  names a different host than the one asked: framatube.org answers with a
  ``media.tube.tchncs.de`` URL because the video lives on its home instance.
  Rebuilding the URL from the watch origin would break exactly the videos
  federation exists for, so a relative value is resolved against the origin only
  as a fallback and an absolute one is never rewritten.

* **Not every video has HLS.** The sample URL the handover named
  (``/w/eJeLCkQyxvK1joGAaBf5PY``) answers with ``streamingPlaylists: []`` and one
  progressive MP4 in ``files[]``, and the instance's own player streams that
  file. So both are published as sources here; which one is downloaded, and at
  which quality, is decided by the application, not by this adapter.

Progressive entries use ``fileUrl`` and never ``fileDownloadUrl``: the download
endpoint exists to make a browser save a file - ``Content-Disposition``, on some
instances a redirect chain and a rate limit - while ``fileUrl`` is the
object-storage URL the player streams from.

Playlist, segments and progressive files are all served from object storage,
outside the API's rate limiter and without auth, cookie or referer - verified
with a header-free request from a cold client - so no source carries headers.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from base_api.models import Media, MediaSource
from base_api.modules.errors import UnsupportedURLError
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

#: `VideoStreamingPlaylistType.HLS` in PeerTube's own enum. The API also returns
#: other playlist kinds, so the entry is selected by this value rather than by
#: position - `streamingPlaylists[0]` is not guaranteed to be the HLS one, and
#: on a video without HLS the list is empty and indexing it would raise.
HLS_PLAYLIST_TYPE = 1

#: Seconds allowed for the single API call. Long enough for a slow federated
#: instance, short enough that a dead host fails the job instead of hanging it.
API_TIMEOUT = 15.0

_SCHEMES = ("http", "https")

# PeerTube's short id is a 22-character base58 form of the UUID; some ids encode
# to 21. Matched exactly, so a path segment that merely looks id-shaped - a
# filename, an `.m3u8`, a slug - cannot be mistaken for one.
_SHORT_UUID = re.compile(r"\A[A-Za-z0-9]{21,22}\Z")
_UUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


class PeerTubeError(Exception):
    """Base class for the adapter's own failures."""


class PeerTubeExtractionError(PeerTubeError):
    """The API answer could not be turned into a playable source.

    Covers every technical failure of the extraction step - transport error,
    timeout, unexpected status, undecodable body, and any answer that does not
    carry the fields this adapter needs. Always raised `from` the original
    exception, so the cause survives without a `KeyError` or a JSON error
    reaching the caller.
    """


class PeerTubeNoSupportedSourceError(PeerTubeExtractionError):
    """The answer was readable, but nothing in it is downloadable.

    Its own type because the two ways to get here need telling apart in a log:
    an instance that published only an audio rendition, and an instance whose
    playlist and files are all unusable. A subclass of the extraction error
    rather than a sibling, so that every caller which already treats "this
    video yielded no source" as an extraction failure keeps working unchanged.
    """


class PeerTubeDownloadDisabledError(PeerTubeError):
    """The instance states the video may not be downloaded.

    Deliberately not an extraction error: nothing failed. `downloadEnabled` is
    the uploader's declared intent, and a retry would return the same answer.
    """


def _watch_video_id(url: str) -> Optional[str]:
    """Return the video id iff `url` is a PeerTube watch URL, else `None`.

    Pure parsing: no network, no side effects, same answer every time. Query and
    fragment are split off by `urlsplit` before anything is inspected, so they
    cannot influence the decision.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        # A malformed authority (a bad IPv6 literal, say) is simply not ours.
        return None

    if parts.scheme not in _SCHEMES or not parts.hostname:
        return None

    # Leading and trailing slashes are noise; an interior empty segment is not,
    # so `/w//id` keeps its empty middle and fails the exact length check below.
    segments = parts.path.strip("/").split("/")

    if len(segments) == 2 and segments[0] == "w":
        candidate = segments[1]
    elif len(segments) == 3 and segments[0] == "videos" and segments[1] == "watch":
        candidate = segments[2]
    else:
        # Everything else on a PeerTube host is deliberately left alone:
        # `/w/p/<id>` is a playlist, `/videos/embed/<id>` an embed, `/c/<name>`
        # a channel, `/a/<name>` an account.
        return None

    if _SHORT_UUID.match(candidate) or _UUID.match(candidate):
        return candidate
    return None


class PeerTubeAdapter:
    """Resolves a PeerTube watch URL into a provider-neutral `Media`.

    The adapter owns the client it extracts with, exactly like the xHamster one:
    the extraction transport must never be the download engine's transport, and
    two adapters must never share a session. Nothing here is global, and no
    header dict is mutated in place.
    """

    def __init__(self, session: Optional[Any] = None) -> None:
        # Created on first use, not here: constructing an adapter - which the
        # registry does for every job - must not open a transport that a
        # `supports()`-only job would never use.
        self._session = session
        self._owns_session = session is None

    def supports(self, url: str) -> bool:
        """Whether this adapter claims `url`. Cheap, synchronous, network-free.

        A watch path on an arbitrary host is claimed without asking that host
        whether it runs PeerTube, because that answer would cost a request and
        this must stay free. The registry treats two claims on one URL as an
        error, which is the guard against a wrong claim mattering.
        """
        return _watch_video_id(url) is not None

    async def resolve(self, url: str) -> Media:
        """Resolve a watch URL into `Media` with every source it offers.

        Both transports are published when the instance has both, and the
        choice between them is not made here: which one gets downloaded, and at
        which quality, is a decision the application makes from the `Media`
        alone. This keeps the adapter a translator - PeerTube's answer into
        provider-neutral sources - with no opinion about transports.
        """
        video_id = _watch_video_id(url)
        if video_id is None:
            raise UnsupportedURLError(f"Not a supported PeerTube watch URL: {url}")

        parts = urlsplit(url)
        # Same origin, same scheme, same port - only the path is replaced, and
        # the watch URL's query and fragment are dropped.
        origin = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
        api_url = urlunsplit(
            (parts.scheme, parts.netloc, f"/api/v1/videos/{video_id}", "", "")
        )

        payload = await self._fetch_video(api_url)

        if not isinstance(payload, dict):
            raise PeerTubeExtractionError(
                f"{api_url} answered with {type(payload).__name__}, not a JSON object"
            )

        # Checked before the metadata: the instance's stated policy decides
        # whether this video may be fetched at all, and a missing title is not
        # the reason a caller should hear when the answer is "not allowed".
        if payload.get("downloadEnabled") is False:
            raise PeerTubeDownloadDisabledError(
                f"The instance has disabled downloads for {url}"
            )

        title = payload.get("name")
        if not isinstance(title, str) or not title.strip():
            raise PeerTubeExtractionError(
                f"{api_url} carries no usable 'name' for {url}"
            )

        sources: list[MediaSource] = []
        playlist_url = self._hls_playlist_url(payload, origin, api_url)
        if playlist_url is not None:
            sources.append(
                MediaSource(
                    url=playlist_url,
                    source_type="HLS",
                    # Measured: the playlist and its segments are served from
                    # object storage and answer a request that carries no
                    # referer, cookie or token at all.
                    headers={},
                )
            )
        sources.extend(self._progressive_sources(payload, api_url))

        if not sources:
            raise PeerTubeNoSupportedSourceError(
                f"{api_url} offers no downloadable source for {url}: no HLS playlist "
                f"and no progressive video file among "
                f"{len(payload.get('files') or []) if isinstance(payload.get('files'), list) else 0} "
                f"'files' entries"
            )

        return Media(
            provider="peertube",
            original_url=url,
            title=title,
            provider_id=video_id,
            sources=sources,
        )

    async def close(self) -> None:
        """Close the owned session. Idempotent, and a no-op for a lent one."""
        if not self._owns_session:
            return
        # Cleared before awaiting, so a second call finds nothing to close even
        # if it arrives while the first is still shutting the session down.
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def _session_for_request(self) -> Any:
        if self._session is None:
            self._session = AsyncSession()
        return self._session

    async def _fetch_video(self, api_url: str) -> Any:
        """GET the video resource and return its decoded body.

        Every failure below leaves as a `PeerTubeExtractionError` carrying its
        cause. `OSError` is the boundary rather than curl_cffi's
        `RequestException`, because that class derives from `OSError` - so this
        catches connection, DNS and both timeout kinds, and still contains a
        plain socket error that reached us unwrapped. `ValueError` covers the
        decode, whose `JSONDecodeError` derives from both.
        """
        session = self._session_for_request()

        try:
            response = await session.get(api_url, timeout=API_TIMEOUT)
        except OSError as exc:
            raise PeerTubeExtractionError(f"Request to {api_url} failed: {exc}") from exc

        status = getattr(response, "status_code", None)
        if status != 200:
            # A watch URL whose id no longer resolves answers 400 here, not 404,
            # so the check is "not the one status we can read" rather than a
            # list of the failures worth naming.
            raise PeerTubeExtractionError(f"{api_url} answered HTTP {status}")

        try:
            return response.json()
        except (OSError, ValueError) as exc:
            raise PeerTubeExtractionError(
                f"{api_url} did not answer with decodable JSON: {exc}"
            ) from exc

    @staticmethod
    def _hls_playlist_url(payload: dict, origin: str, api_url: str) -> Optional[str]:
        """The HLS entry's playlist URL, or `None` if there is no usable one.

        `None` rather than an exception since progressive files exist: a video
        whose `streamingPlaylists` is empty - which is what the sample URL
        actually returns - still has a downloadable MP4, and the decision that
        nothing is downloadable belongs to `resolve()`, which can see both.
        """
        playlists = payload.get("streamingPlaylists")
        if not isinstance(playlists, list):
            logger.debug("%s carries no 'streamingPlaylists' list", api_url)
            return None

        for entry in playlists:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            # `True == 1` in Python, so a boolean is rejected before comparing.
            if isinstance(kind, bool) or kind != HLS_PLAYLIST_TYPE:
                continue

            raw = entry.get("playlistUrl")
            if not isinstance(raw, str) or not raw.strip():
                logger.debug("The HLS entry from %s carries no usable 'playlistUrl'", api_url)
                continue

            # Absolute values - the only kind measured, and the only kind that
            # can be right for a federated video - come back from `urljoin`
            # untouched. A relative one resolves against the instance root.
            resolved = urljoin(origin, raw.strip())
            if urlsplit(resolved).scheme not in _SCHEMES:
                logger.debug("The HLS playlist URL from %s has an unusable scheme", api_url)
                continue
            return resolved

        return None

    @staticmethod
    def _stated_int(value: Any) -> Optional[int]:
        """The integer PeerTube stated, or `None` when it stated none.

        Zero is a value, not an absence - `resolution.id == 0` is how PeerTube
        marks an audio-only file - so the two cases have to stay tellable
        apart. Booleans are not integers here, whatever Python thinks.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @staticmethod
    def _progressive_url(entry: dict) -> Optional[str]:
        """The `fileUrl` of one entry, if this transport may fetch it.

        `fileUrl` and never `fileDownloadUrl`: the download endpoint exists to
        push a browser into saving a file - it answers with a
        `Content-Disposition` and, on some instances, a redirect chain and a
        rate limit that the object-storage URL does not have. `fileUrl` is what
        the instance's own player streams from, and it is the one measured to
        need no header at all.

        Absolute only. A relative `fileUrl` is not something PeerTube produces,
        and resolving one against the watch origin would be a guess about a
        federated video's real host - exactly the mistake the playlist handling
        above exists to avoid.
        """
        raw = entry.get("fileUrl")
        if not isinstance(raw, str) or not raw.strip():
            return None
        candidate = raw.strip()
        try:
            parts = urlsplit(candidate)
            scheme = (parts.scheme or "").lower()
            username, password, hostname = parts.username, parts.password, parts.hostname
        except ValueError:
            return None

        if scheme not in _SCHEMES or not hostname:
            return None
        if username is not None or password is not None:
            # A URL carrying credentials would be logged, persisted into the
            # resume state and replayed on every retry.
            return None
        return candidate

    @classmethod
    def _progressive_source(cls, entry: dict, api_url: str) -> Optional[MediaSource]:
        """Map one `files[]` entry to an HTTP source, or drop it with a reason.

        An entry is dropped only when PeerTube *says* it is not a video:
        `hasVideo` explicitly false, a resolution id of 0, or a height of 0.
        Fields that are simply absent never disqualify anything - `hasVideo`,
        `width` and `height` are all newer than the endpoint and are missing on
        older instances and on federated answers, and treating "not stated" as
        "not a video" would make those videos undownloadable.

        `hasAudio` is never looked at. A silent video is a video.
        """
        if entry.get("hasVideo") is False:
            logger.debug("%s: dropping a files[] entry marked hasVideo=false", api_url)
            return None

        raw_resolution = entry.get("resolution")
        resolution = raw_resolution if isinstance(raw_resolution, dict) else {}
        resolution_id = cls._stated_int(resolution.get("id"))
        height = cls._stated_int(entry.get("height"))

        if resolution_id == 0:
            # PeerTube's own marker for an audio-only rendition.
            logger.debug("%s: dropping a files[] entry with resolution.id 0", api_url)
            return None
        if height == 0:
            logger.debug("%s: dropping a files[] entry with height 0", api_url)
            return None

        url = cls._progressive_url(entry)
        if url is None:
            logger.debug("%s: dropping a files[] entry without a usable fileUrl", api_url)
            return None

        # The numeric tier is PeerTube's own ranking value, with height as the
        # fallback when an older instance states no resolution id. It is never
        # derived from the label or from width x height: a portrait video is
        # `resolution.id = 1920` labelled "1080p", and re-deriving either from
        # the other would silently pick the wrong file.
        if resolution_id is not None and resolution_id > 0:
            quality_value = resolution_id
        elif height is not None and height > 0:
            quality_value = height
        else:
            quality_value = None

        raw_label = resolution.get("label")
        quality_label = (
            raw_label if isinstance(raw_label, str) and raw_label.strip() else None
        )

        size = cls._stated_int(entry.get("size"))
        return MediaSource(
            url=url,
            source_type="HTTP",
            # Measured on video.blender.org: the object-storage URL answers a
            # request from a cold client that carries no referer, cookie or
            # token. Inventing one would be a guess, and a wrong guess here
            # looks exactly like a broken video.
            headers={},
            expected_size=size if size and size > 0 else None,
            quality_value=quality_value,
            # Verbatim. The comparison that uses it normalizes case and
            # surrounding whitespace; the stored value stays the instance's own.
            quality_label=quality_label,
        )

    @classmethod
    def _progressive_sources(cls, payload: dict, api_url: str) -> list[MediaSource]:
        """Every downloadable progressive file, in the order the API listed them."""
        entries = payload.get("files")
        if not isinstance(entries, list):
            return []
        sources = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source = cls._progressive_source(entry, api_url)
            if source is not None:
                sources.append(source)
        return sources
