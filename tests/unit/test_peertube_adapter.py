"""Unit tests for the PeerTube adapter.

Nothing here touches the network. The two fixtures are reductions of real
answers from video.blender.org, so the happy path is checked against a body the
instance actually returned rather than against one written to match the code.

`peertube_video_without_hls.json` is the video the handover named as the sample
URL. It carries no HLS at all - `streamingPlaylists` is empty and the player
streams the progressive MP4 in `files[]` - which makes it a measured rather than
invented case for the progressive path, and a standing argument against indexing
`streamingPlaylists[0]`.

`peertube_video_progressive_variants.json` is the multi-resolution shape, with
the audio-only rendition PeerTube emits alongside the real ones.
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
    PeerTubeNoSupportedSourceError,
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


# --- progressive files ----------------------------------------------------
#
# `files[]` is the other half of a PeerTube answer: standalone MP4s the
# instance's own player streams when there is no HLS playlist. The adapter
# translates them into HTTP sources and nothing more - which one gets
# downloaded is the application's decision, so these tests are about the
# mapping and the filter, never about a preference.


WITHOUT_HLS_WATCH = "https://video.blender.org/w/eJeLCkQyxvK1joGAaBf5PY"
MEASURED_FILE_URL = (
    "https://video.blender.org/object-storage/web_videos/"
    "6f2c86d1-25c1-4f4f-a83e-66dc96be83ee-720.mp4"
)
VARIANTS_WATCH = "https://video.example/w/aB3dEfGhJkLmNpQrStUvWx"


def variants_payload() -> dict:
    return load_fixture("peertube_video_progressive_variants.json")


def video_files(payload: dict) -> list[dict]:
    """The fixture's real video entries, i.e. everything but the audio-only one."""
    return [entry for entry in payload["files"] if entry["resolution"]["id"] != 0]


@pytest.mark.asyncio
async def test_the_measured_video_without_hls_resolves_to_its_progressive_file():
    """The URL the handover named: real, public, and carrying no HLS at all.

    It used to be the proof that "no playlist" fails cleanly. It is now the
    proof that such a video is downloadable, which is what its own player does.
    """
    adapter, _ = adapter_returning(load_fixture("peertube_video_without_hls.json"))

    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert [source.source_type for source in media.sources] == ["HTTP"]
    source = media.sources[0]
    assert source.url == MEASURED_FILE_URL
    assert source.expected_size == 382672246
    assert source.quality_value == 720
    assert source.quality_label == "720p"
    assert source.headers == {}


@pytest.mark.asyncio
async def test_the_progressive_source_uses_file_url_never_file_download_url():
    """`fileDownloadUrl` is the browser-save endpoint, not the stream."""
    payload = load_fixture("peertube_video_without_hls.json")
    download_url = payload["files"][0]["fileDownloadUrl"]

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert media.sources[0].url == MEASURED_FILE_URL
    assert download_url  # the fixture really does offer the other one
    assert all(source.url != download_url for source in media.sources)


@pytest.mark.asyncio
async def test_no_source_carries_a_filename_from_the_api():
    """The output name is the application's; nothing here proposes one."""
    adapter, _ = adapter_returning(load_fixture("peertube_video_without_hls.json"))

    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert not hasattr(media.sources[0], "filename")
    assert media.title == "BCON blues + Q&A #125 | Blender.Today LIVE"


@pytest.mark.asyncio
async def test_a_video_with_only_hls_publishes_only_that():
    adapter, _ = adapter_returning(load_fixture("peertube_video.json"))

    media = await adapter.resolve(WATCH_SHORT)

    assert [source.source_type for source in media.sources] == ["HLS"]
    assert media.sources[0].url == PLAYLIST_URL


@pytest.mark.asyncio
async def test_a_video_with_both_publishes_both_with_hls_first():
    payload = load_fixture("peertube_video.json")
    payload["files"] = variants_payload()["files"]

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    # The audio-only entry is gone; the three real renditions are not.
    assert [source.source_type for source in media.sources] == [
        "HLS", "HTTP", "HTTP", "HTTP"
    ]
    assert media.sources[0].url == PLAYLIST_URL


@pytest.mark.asyncio
async def test_several_progressive_resolutions_all_survive_with_their_metadata():
    adapter, _ = adapter_returning(variants_payload())

    media = await adapter.resolve(VARIANTS_WATCH)

    assert [
        (source.quality_value, source.quality_label, source.expected_size)
        for source in media.sources
    ] == [
        (480, "480p", 104857600),
        (720, "720p", 209715200),
        (1080, "1080p", 419430400),
    ]


@pytest.mark.asyncio
async def test_a_federated_absolute_file_url_survives_untouched():
    """The same rule the playlist has: the home instance names its own host."""
    remote = "https://media.tube.tchncs.de/object-storage/web_videos/abc-720.mp4"
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0]["fileUrl"] = remote

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert media.sources[0].url == remote


@pytest.mark.asyncio
async def test_downloads_disabled_still_wins_over_a_progressive_file():
    """The uploader's stated intent is checked before any source is built."""
    payload = load_fixture("peertube_video_without_hls.json")
    payload["downloadEnabled"] = False

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeDownloadDisabledError):
        await adapter.resolve(WITHOUT_HLS_WATCH)


# --- the candidate filter -------------------------------------------------


@pytest.mark.asyncio
async def test_an_audio_only_entry_is_dropped_next_to_real_video_files():
    payload = variants_payload()
    audio_url = payload["files"][0]["fileUrl"]

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(VARIANTS_WATCH)

    assert all(source.url != audio_url for source in media.sources)
    assert len(media.sources) == 3


@pytest.mark.asyncio
async def test_an_answer_with_nothing_but_audio_has_no_supported_source():
    """Never a fallback: an audio file is not a smaller version of the video."""
    payload = variants_payload()
    payload["files"] = [payload["files"][0]]

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeNoSupportedSourceError):
        await adapter.resolve(VARIANTS_WATCH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mark_as_audio",
    [
        pytest.param(lambda entry: entry.__setitem__("hasVideo", False), id="hasVideo-false"),
        pytest.param(
            lambda entry: entry["resolution"].__setitem__("id", 0), id="resolution-id-0"
        ),
        pytest.param(lambda entry: entry.__setitem__("height", 0), id="height-0"),
    ],
)
async def test_each_explicit_not_a_video_marker_drops_the_entry(mark_as_audio):
    payload = load_fixture("peertube_video_without_hls.json")
    mark_as_audio(payload["files"][0])

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeNoSupportedSourceError):
        await adapter.resolve(WITHOUT_HLS_WATCH)


@pytest.mark.asyncio
async def test_a_silent_video_is_still_a_video():
    """`hasAudio` is never a filter: a video with no sound is a video."""
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0]["hasAudio"] = False

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert [source.url for source in media.sources] == [MEASURED_FILE_URL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutate", "expected_quality"),
    [
        pytest.param(lambda entry: entry.pop("hasVideo"), 720, id="hasVideo-missing"),
        pytest.param(lambda entry: entry.__setitem__("width", None), 720, id="width-null"),
        pytest.param(lambda entry: entry.pop("width"), 720, id="width-missing"),
        pytest.param(lambda entry: entry.pop("height"), 720, id="height-missing"),
        pytest.param(
            lambda entry: entry.__setitem__("height", None), 720, id="height-null"
        ),
        # No resolution id: the height carries the tier instead.
        pytest.param(lambda entry: entry.pop("resolution"), 720, id="resolution-missing"),
        pytest.param(
            lambda entry: entry["resolution"].pop("id"), 720, id="resolution-id-missing"
        ),
        pytest.param(
            lambda entry: entry["resolution"].__setitem__("id", None),
            720,
            id="resolution-id-null",
        ),
    ],
)
async def test_absent_metadata_never_disqualifies_a_candidate(mutate, expected_quality):
    """These fields are newer than the endpoint; missing is not "not a video"."""
    payload = load_fixture("peertube_video_without_hls.json")
    mutate(payload["files"][0])

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert [source.url for source in media.sources] == [MEASURED_FILE_URL]
    assert media.sources[0].quality_value == expected_quality


@pytest.mark.asyncio
async def test_an_entry_with_no_usable_tier_at_all_is_still_offered():
    payload = load_fixture("peertube_video_without_hls.json")
    entry = payload["files"][0]
    entry.pop("resolution")
    entry.pop("height")

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert media.sources[0].quality_value is None
    assert media.sources[0].quality_label is None
    assert media.sources[0].expected_size == 382672246


@pytest.mark.asyncio
async def test_a_missing_size_leaves_the_expected_size_unstated():
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0].pop("size")

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert media.sources[0].expected_size is None


@pytest.mark.asyncio
async def test_a_portrait_video_keeps_the_instances_own_tier_and_label():
    """The case the two fields exist for.

    PeerTube ranks a portrait video by its long side, so a 1080x1920 video is
    `resolution.id = 1920` and `label = "1080p"`. Both are stored as they
    arrived: re-deriving either from the other would pick the wrong file.
    """
    payload = load_fixture("peertube_video_without_hls.json")
    entry = payload["files"][0]
    entry["resolution"] = {"id": 1920, "label": "1080p"}
    entry["width"] = 1080
    entry["height"] = 1920

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WITHOUT_HLS_WATCH)

    assert media.sources[0].quality_value == 1920
    assert media.sources[0].quality_label == "1080p"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_url",
    [
        pytest.param("javascript:alert(1)", id="hostile-scheme"),
        pytest.param("file:///etc/passwd", id="file-scheme"),
        pytest.param("ftp://video.example/x.mp4", id="ftp-scheme"),
        pytest.param("https://user:secret@video.example/x.mp4", id="credentials"),
        pytest.param("https://user@video.example/x.mp4", id="userinfo"),
        pytest.param("/object-storage/web_videos/x.mp4", id="relative"),
        pytest.param("web_videos/x.mp4", id="relative-bare"),
        pytest.param("https:///x.mp4", id="no-host"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(None, id="null"),
        pytest.param(42, id="not-a-string"),
    ],
)
async def test_a_file_url_this_application_may_not_fetch_is_dropped(file_url):
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0]["fileUrl"] = file_url

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeNoSupportedSourceError):
        await adapter.resolve(WITHOUT_HLS_WATCH)


@pytest.mark.asyncio
async def test_a_rejected_file_url_never_falls_back_to_the_download_endpoint():
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0]["fileUrl"] = "javascript:alert(1)"

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeNoSupportedSourceError):
        await adapter.resolve(WITHOUT_HLS_WATCH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "files",
    [
        pytest.param([], id="empty-list"),
        pytest.param(None, id="null"),
        pytest.param({}, id="not-a-list"),
        pytest.param(["not an object"], id="entries-not-objects"),
    ],
)
async def test_an_unusable_files_value_leaves_the_video_without_a_source(files):
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"] = files

    adapter, _ = adapter_returning(payload)

    with pytest.raises(PeerTubeNoSupportedSourceError):
        await adapter.resolve(WITHOUT_HLS_WATCH)


@pytest.mark.asyncio
async def test_an_unusable_playlist_still_yields_the_progressive_files():
    """A broken playlist is not a reason to ignore a working MP4."""
    payload = load_fixture("peertube_video.json")
    payload["streamingPlaylists"][0]["playlistUrl"] = "javascript:alert(1)"
    payload["files"] = variants_payload()["files"]

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    assert [source.source_type for source in media.sources] == ["HTTP", "HTTP", "HTTP"]


@pytest.mark.asyncio
async def test_two_resolutions_do_not_share_one_header_dict():
    adapter, _ = adapter_returning(variants_payload())

    media = await adapter.resolve(VARIANTS_WATCH)
    media.sources[0].headers["Referer"] = "https://example.invalid/"

    assert media.sources[1].headers == {}


# --- what the instance states about the media itself -----------------------


@pytest.mark.asyncio
async def test_a_progressive_source_carries_what_the_instance_stated():
    payload = load_fixture("peertube_video_without_hls.json")
    entry = payload["files"][0]

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)
    track = media.sources[0].track

    assert track.role == "combined"
    assert track.container == "mp4"
    assert track.fps == entry["fps"]
    assert track.width == entry["width"]
    assert track.height == entry["height"]


@pytest.mark.asyncio
async def test_what_peertube_never_states_stays_unset():
    """The endpoint names no codec, bitrate, language or dynamic-range variant.

    Deriving any of those from the file name would read as a fact once it is in
    the model, and a caller choosing between renditions would act on a guess.
    """
    adapter, _ = adapter_returning(load_fixture("peertube_video_without_hls.json"))
    media = await adapter.resolve(WATCH_SHORT)
    track = media.sources[0].track

    assert track.video_codec is None
    assert track.audio_codec is None
    assert track.bitrate_bps is None
    assert track.language is None
    assert track.is_default_audio is None
    assert track.is_dynamic_range_compressed is None
    assert track.duration_ms is None


@pytest.mark.asyncio
async def test_an_entry_that_asserts_neither_flag_gets_no_role():
    """An older instance states no hasVideo/hasAudio; "probably both" is a guess."""
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0].pop("hasVideo", None)
    payload["files"][0].pop("hasAudio", None)

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    assert media.sources[0].track.role is None


@pytest.mark.asyncio
async def test_an_entry_without_fps_leaves_fps_unset_rather_than_zero():
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0].pop("fps", None)

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    assert media.sources[0].track.fps is None


@pytest.mark.asyncio
async def test_the_container_comes_from_the_path_and_ignores_a_query_string():
    """Object storage can presign; a signature is not part of the file name."""
    payload = load_fixture("peertube_video_without_hls.json")
    payload["files"][0]["fileUrl"] = (
        "https://s3.example/bucket/9d3c1f52-720.mp4"
        "?X-Amz-Signature=DEADBEEF&X-Amz-Expires=900"
    )

    adapter, _ = adapter_returning(payload)
    media = await adapter.resolve(WATCH_SHORT)

    assert media.sources[0].track.container == "mp4"


@pytest.mark.asyncio
async def test_two_resolutions_do_not_share_one_track_object():
    adapter, _ = adapter_returning(variants_payload())

    media = await adapter.resolve(VARIANTS_WATCH)
    media.sources[0].track.container = "webm"

    assert media.sources[1].track.container == "mp4"
