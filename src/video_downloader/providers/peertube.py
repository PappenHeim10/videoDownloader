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

Playlist and segments are served from object storage, outside the API's rate
limiter and without auth, cookie or referer - verified with a header-free
request from a cold client - so the source carries no headers.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from base_api.models import Media, MediaSource
from base_api.modules.errors import UnsupportedURLError
from curl_cffi.requests import AsyncSession

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
        """Resolve a watch URL into `Media` with a single HLS source."""
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

        playlist_url = self._hls_playlist_url(payload, origin, api_url)

        return Media(
            provider="peertube",
            original_url=url,
            title=title,
            provider_id=video_id,
            sources=[
                MediaSource(
                    url=playlist_url,
                    source_type="HLS",
                    # Measured: the playlist and its segments are served from
                    # object storage and answer a request that carries no
                    # referer, cookie or token at all.
                    headers={},
                )
            ],
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
    def _hls_playlist_url(payload: dict, origin: str, api_url: str) -> str:
        """Pick the HLS entry's playlist URL out of an already-decoded body."""
        playlists = payload.get("streamingPlaylists")
        if not isinstance(playlists, list):
            raise PeerTubeExtractionError(
                f"{api_url} carries no 'streamingPlaylists' list"
            )

        saw_hls_entry = False
        for entry in playlists:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("type")
            # `True == 1` in Python, so a boolean is rejected before comparing.
            if isinstance(kind, bool) or kind != HLS_PLAYLIST_TYPE:
                continue
            saw_hls_entry = True

            raw = entry.get("playlistUrl")
            if not isinstance(raw, str) or not raw.strip():
                continue

            # Absolute values - the only kind measured, and the only kind that
            # can be right for a federated video - come back from `urljoin`
            # untouched. A relative one resolves against the instance root.
            resolved = urljoin(origin, raw.strip())
            if urlsplit(resolved).scheme not in _SCHEMES:
                continue
            return resolved

        if saw_hls_entry:
            raise PeerTubeExtractionError(
                f"The HLS entry from {api_url} carries no usable 'playlistUrl'"
            )
        raise PeerTubeExtractionError(
            f"{api_url} offers no HLS playlist (type {HLS_PLAYLIST_TYPE})"
        )
