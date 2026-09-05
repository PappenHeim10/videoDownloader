"""Unit tests for the PeerTube adapter.

Nothing here touches the network. The two fixtures are reductions of real
answers from video.blender.org, so the happy path is checked against a body the
instance actually returned rather than against one written to match the code.

`peertube_video_without_hls.json` is the video the handover named as the sample
URL. It turned out to carry no HLS at all - `streamingPlaylists` is empty and the
player falls back to a progressive MP4 - which makes it a measured rather than
invented case for the "no HLS" path, and a standing argument against indexing
`streamingPlaylists[0]`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from base_api.modules.errors import UnsupportedURLError
from base_api.provider import MediaProvider
from curl_cffi.requests.exceptions import ConnectTimeout, DNSError

from video_downloader.providers import peertube
from video_downloader.providers.peertube import (
    PeerTubeAdapter,
    PeerTubeDownloadDisabledError,
    PeerTubeExtractionError,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

WATCH_SHORT = "https://video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z"
WATCH_UUID = "https://video.blender.org/videos/watch/c1c8d764-de9b-4800-9274-5a26ac7db66b"
API_SHORT = "https://video.blender.org/api/v1/videos/pVUiwGhkrrwWqW7jyHer4z"
PLAYLIST_URL = (
    "https://video.blender.org/object-storage/streaming_playlists/hls/"
    "c1c8d764-de9b-4800-9274-5a26ac7db66b/"
    "6428d93f-70e9-457d-9d64-ace4c7d6eea9-master.m3u8"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload=None, status_code=200, raises=None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class FakeSession:
    """Stands in for the curl_cffi session, and records what it was asked for."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.requests: list[tuple[str, dict]] = []
        self.close_calls = 0

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response

    async def close(self):
        self.close_calls += 1


class ExplodingSession:
    """Any use at all is a failure - used to prove `supports()` stays offline."""

    async def get(self, url, **kwargs):
        raise AssertionError(f"supports() must not perform a request, got {url}")

    async def close(self):
        raise AssertionError("supports() must not close anything")


def adapter_returning(payload, status_code=200, raises=None, json_raises=None):
    session = FakeSession(
        response=FakeResponse(payload, status_code=status_code, raises=json_raises),
        raises=raises,
    )
    return PeerTubeAdapter(session=session), session


# --- contract -------------------------------------------------------------


def test_the_adapter_satisfies_the_provider_contract():
    assert isinstance(PeerTubeAdapter(), MediaProvider)


# --- supports(): the URLs this adapter claims -----------------------------


@pytest.mark.parametrize(
    "url",
    [
        WATCH_SHORT,
        WATCH_UUID,
        "http://peertube.local/w/pVUiwGhkrrwWqW7jyHer4z",
        "https://framatube.org/w/pVUiwGhkrrwWqW7jyHer4z",
        # A port and a trailing slash change nothing about the path shape.
        "https://peertube.local:9000/w/pVUiwGhkrrwWqW7jyHer4z",
        "https://video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z/",
        "https://video.blender.org/w/c1c8d764-de9b-4800-9274-5a26ac7db66b",
    ],
)
def test_a_watch_url_is_claimed(url):
    assert PeerTubeAdapter().supports(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # Another provider's URL, and the raw manifest the direct adapter owns.
        "https://xhamster.com/videos/some-title-12345678",
        "https://xhamster.desi/moments/1234",
        PLAYLIST_URL,
        "https://video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z.m3u8",
        # Other PeerTube surfaces that are not a single playable video.
        "https://video.blender.org/videos/embed/pVUiwGhkrrwWqW7jyHer4z",
        "https://video.blender.org/w/p/mhch7WhpsGtxHmqZC6Uphs",
        "https://video.blender.org/c/blender_channel",
        "https://video.blender.org/a/blender",
        "https://video.blender.org/videos/browse",
        # Wrong number of path segments, either way.
        "https://video.blender.org/w",
        "https://video.blender.org/w/",
        "https://video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z/extra",
        "https://video.blender.org/w//pVUiwGhkrrwWqW7jyHer4z",
        "https://video.blender.org/videos/watch",
        "https://video.blender.org/videos/watch/c1c8d764-de9b-4800-9274-5a26ac7db66b/x",
        "https://video.blender.org/",
        # An id-shaped segment that is not an id shape we accept.
        "https://video.blender.org/w/short",
        "https://video.blender.org/w/not-a-uuid-at-all-really-no",
        # Schemes the adapter must never claim.
        "ftp://video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z",
        "file:///w/pVUiwGhkrrwWqW7jyHer4z",
        "peertube://video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z",
        "//video.blender.org/w/pVUiwGhkrrwWqW7jyHer4z",
        "https:///w/pVUiwGhkrrwWqW7jyHer4z",
        "not a url",
        "",
    ],
)
def test_a_foreign_or_malformed_url_is_not_claimed(url):
    assert PeerTubeAdapter().supports(url) is False


@pytest.mark.parametrize(
    "url",
    [
        WATCH_SHORT + "?start=42",
        WATCH_SHORT + "#t=90",
        WATCH_SHORT + "?a=1&b=2#fragment",
        # A query that would flip the decision if the path were matched loosely.
        WATCH_SHORT + "?next=/videos/embed/pVUiwGhkrrwWqW7jyHer4z",
    ],
)
def test_query_and_fragment_do_not_change_the_decision(url):
    assert PeerTubeAdapter().supports(url) is True


def test_supports_never_reaches_the_network():
    adapter = PeerTubeAdapter(session=ExplodingSession())

    for url in (WATCH_SHORT, WATCH_UUID, PLAYLIST_URL, "https://xhamster.com/videos/x"):
        adapter.supports(url)

    # A lent session is the only one this adapter could have used; an adapter
    # left to itself must not have built one either.
    assert PeerTubeAdapter()._session is None


# --- resolve(): the request it makes --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url, expected_api",
    [
        (WATCH_SHORT, API_SHORT),
        (
            WATCH_UUID,
            "https://video.blender.org/api/v1/videos/"
            "c1c8d764-de9b-4800-9274-5a26ac7db66b",
        ),
        # Query and fragment belong to the watch page, not to the API call.
        (WATCH_SHORT + "?start=42#t=90", API_SHORT),
        (
            "https://peertube.local:9000/w/pVUiwGhkrrwWqW7jyHer4z",
            "https://peertube.local:9000/api/v1/videos/pVUiwGhkrrwWqW7jyHer4z",
        ),
    ],
)
async def test_resolve_builds_the_api_endpoint_on_the_same_origin(url, expected_api):
    adapter, session = adapter_returning(load_fixture("peertube_video.json"))

    await adapter.resolve(url)

    assert [request_url for request_url, _ in session.requests] == [expected_api]


@pytest.mark.asyncio
async def test_an_unsupported_url_is_refused_before_any_request():
    adapter, session = adapter_returning(load_fixture("peertube_video.json"))

    with pytest.raises(UnsupportedURLError):
        await adapter.resolve("https://xhamster.com/videos/some-title-12345678")

    assert session.requests == []


# --- resolve(): the mapping -----------------------------------------------


@pytest.mark.asyncio
async def test_the_measured_answer_maps_onto_one_hls_source():
    adapter, _ = adapter_returning(load_fixture("peertube_video.json"))

    media = await adapter.resolve(WATCH_SHORT)

    assert media.provider == "peertube"
    assert media.title == "Blender 5.3 is UNFAIR - Blender Today LIVE #288"
    assert media.original_url == WATCH_SHORT
    assert media.provider_id == "pVUiwGhkrrwWqW7jyHer4z"
    assert len(media.sources) == 1

    source = media.sources[0]
    assert source.source_type == "HLS"
    assert source.url == PLAYLIST_URL
    assert source.headers == {}


@pytest.mark.asyncio
async def test_the_hls_entry_is_chosen_by_type_not_by_position():
    payload = load_fixture("peertube_video.json")
    # Type 0 is PeerTube's other playlist kind; it sits first on purpose.
    payload["streamingPlaylists"].insert(
        0, {"id": 1, "type": 0, "playlistUrl": "https://video.blender.org/wrong.m3u8"}
    )

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    assert media.sources[0].url == PLAYLIST_URL


@pytest.mark.asyncio
async def test_a_playlist_url_on_another_host_survives_untouched():
    """Federated videos are served by their home instance, not by the one asked."""
    payload = load_fixture("peertube_video.json")
    remote = "https://media.tube.tchncs.de/streaming-playlists/hls/x/y-master.m3u8"
    payload["streamingPlaylists"][0]["playlistUrl"] = remote

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve("https://framatube.org/w/pVUiwGhkrrwWqW7jyHer4z")

    assert media.sources[0].url == remote


@pytest.mark.asyncio
async def test_a_relative_playlist_url_resolves_against_the_instance_origin():
    payload = load_fixture("peertube_video.json")
    payload["streamingPlaylists"][0]["playlistUrl"] = "/static/hls/x/master.m3u8"

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    assert media.sources[0].url == "https://video.blender.org/static/hls/x/master.m3u8"


# --- resolve(): the error contract ----------------------------------------


@pytest.mark.asyncio
async def test_a_video_with_downloads_disabled_is_reported_as_such():
    payload = load_fixture("peertube_video.json")
    payload["downloadEnabled"] = False

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeDownloadDisabledError):
        await adapter.resolve(WATCH_SHORT)


@pytest.mark.asyncio
async def test_the_measured_video_without_any_playlist_fails_cleanly():
    """The URL the handover named: real, public, and carrying no HLS at all."""
    adapter, _ = adapter_returning(load_fixture("peertube_video_without_hls.json"))

    with pytest.raises(PeerTubeExtractionError):
        await adapter.resolve("https://video.blender.org/w/eJeLCkQyxvK1joGAaBf5PY")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("streamingPlaylists"), id="key-missing"),
        pytest.param(
            lambda p: p.__setitem__("streamingPlaylists", None), id="not-a-list"
        ),
        pytest.param(
            lambda p: p.__setitem__("streamingPlaylists", [{"id": 1, "type": 0}]),
            id="no-hls-entry",
        ),
        pytest.param(
            lambda p: p["streamingPlaylists"][0].pop("playlistUrl"), id="url-missing"
        ),
        pytest.param(
            lambda p: p["streamingPlaylists"][0].__setitem__("playlistUrl", ""),
            id="url-empty",
        ),
        pytest.param(
            lambda p: p["streamingPlaylists"][0].__setitem__("playlistUrl", "   "),
            id="url-blank",
        ),
        pytest.param(
            lambda p: p["streamingPlaylists"][0].__setitem__("playlistUrl", None),
            id="url-null",
        ),
        pytest.param(
            lambda p: p["streamingPlaylists"][0].__setitem__(
                "playlistUrl", "javascript:alert(1)"
            ),
            id="url-hostile-scheme",
        ),
        pytest.param(lambda p: p.pop("name"), id="name-missing"),
        pytest.param(lambda p: p.__setitem__("name", ""), id="name-empty"),
        pytest.param(lambda p: p.__setitem__("name", "   "), id="name-blank"),
        pytest.param(lambda p: p.__setitem__("name", None), id="name-null"),
        pytest.param(lambda p: p.__setitem__("name", 42), id="name-not-a-string"),
    ],
)
async def test_an_incomplete_answer_becomes_an_extraction_error(mutate):
    payload = load_fixture("peertube_video.json")
    mutate(payload)

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeExtractionError):
        await adapter.resolve(WATCH_SHORT)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 503])
async def test_an_unexpected_http_status_becomes_an_extraction_error(status):
    adapter, _ = adapter_returning({"name": "x"}, status_code=status)

    with pytest.raises(PeerTubeExtractionError) as caught:
        await adapter.resolve(WATCH_SHORT)

    assert str(status) in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        json.JSONDecodeError("Expecting value", "<html>", 0),
        ValueError("not json"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
)
async def test_an_undecodable_body_becomes_an_extraction_error(error):
    adapter, _ = adapter_returning(None, json_raises=error)

    with pytest.raises(PeerTubeExtractionError) as caught:
        await adapter.resolve(WATCH_SHORT)

    assert caught.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ConnectTimeout("timed out"),
        DNSError("could not resolve host"),
        OSError("connection reset"),
    ],
)
async def test_a_transport_failure_becomes_an_extraction_error(error):
    adapter, _ = adapter_returning(None, raises=error)

    with pytest.raises(PeerTubeExtractionError) as caught:
        await adapter.resolve(WATCH_SHORT)

    assert caught.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], "a string", 42])
async def test_a_body_that_is_not_an_object_becomes_an_extraction_error(payload):
    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeExtractionError):
        await adapter.resolve(WATCH_SHORT)


# --- lifecycle ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_adapter_closes_the_session_it_owns_exactly_once(monkeypatch):
    built: list[FakeSession] = []

    def build_session():
        session = FakeSession(response=FakeResponse(load_fixture("peertube_video.json")))
        built.append(session)
        return session

    monkeypatch.setattr(peertube, "AsyncSession", build_session)

    adapter = PeerTubeAdapter()
    await adapter.resolve(WATCH_SHORT)
    assert len(built) == 1

    await adapter.close()
    await adapter.close()

    assert built[0].close_calls == 1


@pytest.mark.asyncio
async def test_closing_an_adapter_that_never_ran_is_harmless(monkeypatch):
    monkeypatch.setattr(
        peertube, "AsyncSession", lambda: pytest.fail("no session should be built")
    )

    adapter = PeerTubeAdapter()
    await adapter.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_a_lent_session_is_never_closed_by_the_adapter():
    """The extraction client must not be shared - and what is lent is not ours."""
    adapter, session = adapter_returning(load_fixture("peertube_video.json"))

    await adapter.resolve(WATCH_SHORT)
    await adapter.close()
    await adapter.close()

    assert session.close_calls == 0


@pytest.mark.asyncio
async def test_two_adapters_never_share_a_session(monkeypatch):
    monkeypatch.setattr(peertube, "AsyncSession", lambda: FakeSession())

    first, second = PeerTubeAdapter(), PeerTubeAdapter()

    assert first._session_for_request() is not second._session_for_request()


@pytest.mark.asyncio
async def test_the_source_headers_are_the_media_s_own_dict():
    """Two resolutions must not hand out one aliased header dict."""
    adapter, _ = adapter_returning(load_fixture("peertube_video.json"))

    first = await adapter.resolve(WATCH_SHORT)
    second = await adapter.resolve(WATCH_SHORT)
    first.sources[0].headers["Referer"] = "https://example.invalid/"

    assert second.sources[0].headers == {}
