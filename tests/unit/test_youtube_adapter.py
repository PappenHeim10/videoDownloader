"""The YouTube adapter: which URLs it claims, and what it makes of an answer.

Nothing here touches the network. The fixtures are real yt-dlp answers with the
signed media URLs replaced by synthetic ones - the only thing about them that
could not go into a repository - and reduced to the fields the adapter reads.

The cases that carry weight are the ones where the plausible shortcut is wrong:
a portrait Short whose height contradicts its label, forty-five audio renditions
of which one is the original, and the flags that must stay `None` on a video
that says nothing about them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from base_api.modules.errors import UnsupportedURLError
from base_api.provider import MediaProvider

from video_downloader.providers.youtube import (
    YouTubeAdapter,
    YouTubeExtractionError,
    YouTubeLiveNotSupportedError,
    YouTubeNoSupportedSourceError,
    YouTubePlayerContractError,
    YouTubeUnavailableError,
    YouTubeUnsupportedTargetError,
    _canonical_video_id,
    base_options,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
VIDEO_ID = "tEwb4cuFjKE"
SHORT_ID = "YX9duUGcDxI"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def adapter_for(name: str) -> YouTubeAdapter:
    payload = load(name)
    return YouTubeAdapter(resolver=lambda url: payload)


def adapter_raising(error: Exception) -> YouTubeAdapter:
    def resolver(url: str):
        raise error

    return YouTubeAdapter(resolver=resolver)


# --- contract --------------------------------------------------------------


def test_the_adapter_satisfies_the_provider_contract():
    assert isinstance(YouTubeAdapter(), MediaProvider)


# --- which URLs are ours ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=tEwb4cuFjKE",
        "https://youtube.com/watch?v=tEwb4cuFjKE",
        "https://m.youtube.com/watch?v=tEwb4cuFjKE",
        "https://music.youtube.com/watch?v=tEwb4cuFjKE",
        "https://youtu.be/tEwb4cuFjKE",
        "https://www.youtube.com/shorts/tEwb4cuFjKE",
        "https://www.youtube.com/embed/tEwb4cuFjKE",
        "https://www.youtube.com/v/tEwb4cuFjKE",
        "https://www.youtube.com/live/tEwb4cuFjKE",
        "https://www.youtube-nocookie.com/embed/tEwb4cuFjKE",
    ],
)
def test_every_single_video_form_is_claimed(url):
    assert YouTubeAdapter().supports(url) is True
    assert _canonical_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=tEwb4cuFjKE&list=PLabc&index=2",
        "https://youtu.be/tEwb4cuFjKE?t=30",
        "https://www.youtube.com/watch?app=desktop&v=tEwb4cuFjKE",
    ],
)
def test_extra_query_parameters_do_not_change_the_id(url):
    assert _canonical_video_id(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/watch?v=tEwb4cuFjKE",
        "https://example.com/watch?v=tEwb4cuFjKE",
        "ftp://youtube.com/watch?v=tEwb4cuFjKE",
        "https://www.youtube.com/watch?v=tooshort",
        "https://www.youtube.com/watch?v=waaaaaaaaaaytoolong",
        "https://www.youtube.com/watch",
        "https://www.youtube.com/",
        "https://video.blender.org/w/eJeLCkQyxvK1joGAaBf5PY",
        "not a url at all",
    ],
)
def test_a_foreign_or_malformed_url_is_not_claimed(url):
    assert YouTubeAdapter().supports(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/channel/UCEeL4jELzooI7cyrouQzoJg",
        "https://www.youtube.com/@LittleJoel",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/results?search_query=cats",
    ],
)
def test_a_collection_url_is_claimed_so_the_refusal_can_say_why(url):
    """"Playlists are not supported" is a better sentence than "unsupported URL"."""
    assert YouTubeAdapter().supports(url) is True
    with pytest.raises(YouTubeUnsupportedTargetError):
        _canonical_video_id(url)


def test_supports_never_reaches_the_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("supports() must not resolve anything")

    monkeypatch.setattr(YouTubeAdapter, "_extract", explode)
    assert YouTubeAdapter().supports("https://www.youtube.com/watch?v=tEwb4cuFjKE")


# --- what an answer becomes ------------------------------------------------


@pytest.mark.asyncio
async def test_a_video_resolves_into_tracks_with_stable_identities():
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    assert media.provider == "youtube"
    assert media.provider_id == VIDEO_ID
    assert media.title
    assert media.sources
    for source in media.sources:
        # Fetched by the resolver that produced the URL, not by the engine's
        # transport - see the C01 gate. `source_type` names the transport.
        assert source.source_type == "YTDLP"
        assert source.identity is not None
        assert source.identity.startswith(f"youtube:{VIDEO_ID}:")


@pytest.mark.asyncio
async def test_codec_strings_arrive_verbatim():
    """The adapter stores what the resolver said; families are decided later."""
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    codecs = {s.track.video_codec for s in media.sources if s.track.video_codec}
    assert any(codec.startswith("avc1.") for codec in codecs)
    assert "avc1" not in codecs
    assert "h264" not in codecs


@pytest.mark.asyncio
async def test_roles_are_taken_from_the_codecs_the_resolver_reported():
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    roles = {source.track.role for source in media.sources}
    assert roles <= {"video", "audio", "combined"}
    assert "video" in roles and "audio" in roles

    for source in media.sources:
        if source.track.role == "video":
            assert source.track.audio_codec is None
        if source.track.role == "audio":
            assert source.track.video_codec is None


@pytest.mark.asyncio
async def test_the_tier_comes_from_youtubes_label_not_from_the_height():
    """The portrait Short: 1080x1920, and YouTube calls it 1080p."""
    media = await adapter_for("youtube_short_multi_audio.json").resolve(
        f"https://www.youtube.com/shorts/{SHORT_ID}"
    )

    portrait = [
        source for source in media.sources
        if source.track.height == 1920 and source.track.width == 1080
    ]
    assert portrait, "the fixture should contain the portrait renditions"
    for source in portrait:
        assert source.quality_value == 1080
        assert source.quality_label == "1080p"


@pytest.mark.asyncio
async def test_a_60fps_label_is_kept_whole_and_its_tier_parsed_out():
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    for source in media.sources:
        if source.quality_label and source.quality_label.endswith("60"):
            assert source.quality_value == int(source.quality_label[:-3])


@pytest.mark.asyncio
async def test_only_one_audio_rendition_is_marked_default_on_a_dubbed_video():
    """Forty-five renditions; the flag is the only thing that tells them apart."""
    media = await adapter_for("youtube_short_multi_audio.json").resolve(
        f"https://www.youtube.com/shorts/{SHORT_ID}"
    )

    audio = [s for s in media.sources if s.track.role == "audio"]
    assert len(audio) > 10, "the fixture should carry the dubbed renditions"

    defaults = {s.track.language for s in audio if s.track.is_default_audio is True}
    others = {s.track.is_default_audio for s in audio if s.track.language not in defaults}

    assert len(defaults) == 1
    assert others == {False}, "a non-default track must say False, not None"


@pytest.mark.asyncio
async def test_a_video_with_one_audio_language_says_nothing_about_defaults():
    """`None` means "no such concept here" and must not become `False`."""
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    audio = [s for s in media.sources if s.track.role == "audio"]
    assert audio
    assert all(source.track.is_default_audio is None for source in audio)


@pytest.mark.asyncio
async def test_drc_renditions_are_marked_and_the_others_are_marked_not_drc():
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    audio = [s for s in media.sources if s.track.role == "audio"]
    flags = {source.track.is_dynamic_range_compressed for source in audio}
    assert flags == {True, False}, "the fixture carries both, so both must be stated"


@pytest.mark.asyncio
async def test_no_track_claims_a_duration_youtube_never_stated():
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    assert all(source.track.duration_ms is None for source in media.sources)


@pytest.mark.asyncio
async def test_headers_are_carried_per_source_and_carry_no_cookie():
    media = await adapter_for("youtube_video.json").resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    for source in media.sources:
        assert "Cookie" not in source.headers and "cookie" not in source.headers

    media.sources[0].headers["Referer"] = "https://example.invalid/"
    assert "Referer" not in media.sources[1].headers


@pytest.mark.asyncio
async def test_a_storyboard_or_manifest_entry_is_not_offered_as_a_track():
    payload = load("youtube_video.json")
    payload["formats"].append(
        {"format_id": "sb0", "ext": "mhtml", "protocol": "mhtml",
         "vcodec": "none", "acodec": "none", "url": "https://media.test/sb.mhtml"}
    )
    payload["formats"].append(
        {"format_id": "hls-1", "ext": "mp4", "protocol": "m3u8_native",
         "vcodec": "avc1.4d401f", "acodec": "none", "url": "https://media.test/x.m3u8"}
    )

    media = await YouTubeAdapter(resolver=lambda url: payload).resolve(
        f"https://www.youtube.com/watch?v={VIDEO_ID}"
    )

    assert all("mhtml" not in source.url for source in media.sources)
    assert all(".m3u8" not in source.url for source in media.sources)


# --- failure ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unsupported_url_is_refused_before_anything_is_resolved():
    with pytest.raises(UnsupportedURLError):
        await YouTubeAdapter(resolver=lambda url: {}).resolve("https://example.com/x")


@pytest.mark.asyncio
async def test_a_live_stream_is_refused_without_a_media_request():
    with pytest.raises(YouTubeLiveNotSupportedError):
        await adapter_for("youtube_live.json").resolve(
            "https://www.youtube.com/watch?v=hESFZeam7Gc"
        )


@pytest.mark.asyncio
async def test_a_video_with_no_fetchable_format_is_reported_as_such():
    payload = load("youtube_video.json")
    payload["formats"] = [
        {"format_id": "sb0", "ext": "mhtml", "protocol": "mhtml",
         "vcodec": "none", "acodec": "none", "url": "https://media.test/sb.mhtml"}
    ]

    with pytest.raises(YouTubeNoSupportedSourceError):
        await YouTubeAdapter(resolver=lambda url: payload).resolve(
            f"https://www.youtube.com/watch?v={VIDEO_ID}"
        )


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_video_description_is_an_extraction_error():
    with pytest.raises(YouTubeExtractionError):
        await YouTubeAdapter(resolver=lambda url: None).resolve(
            f"https://www.youtube.com/watch?v={VIDEO_ID}"
        )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Sign in to confirm you're not a bot", YouTubeUnavailableError),
        ("Private video. Sign in if you've been granted access", YouTubeUnavailableError),
        ("Join this channel to get access to members-only content", YouTubeUnavailableError),
        ("Video unavailable", YouTubeExtractionError),
        ("This video is not available in your country", YouTubeUnavailableError),
        ("Some formats may be missing: no JavaScript runtime found", YouTubePlayerContractError),
        ("Failed to extract nsig function", YouTubePlayerContractError),
        ("Something nobody has seen before", YouTubeExtractionError),
    ],
)
def test_a_resolver_message_becomes_the_failure_this_application_names(message, expected):
    assert isinstance(YouTubeAdapter._classify(message, RuntimeError(message)), expected)


# --- privacy ---------------------------------------------------------------


def test_the_resolver_is_never_asked_to_be_verbose_or_to_read_cookies():
    options = base_options()

    assert options["verbose"] is False
    assert options["cookiefile"] is None
    assert options["cookiesfrombrowser"] is None
    assert options["cachedir"] is False
    assert options["postprocessors"] == []
    assert options["writeinfojson"] is False
    assert options["logger"] is not None


def test_verbose_cannot_be_switched_on_through_the_overrides():
    """The debug build raises the application's log level, never the resolver's."""
    options = base_options(skip_download=True)

    assert options["verbose"] is False


def test_the_injected_logger_redacts_a_url_before_it_reaches_the_log(caplog):
    signed = (
        "https://rr2---sn-x.googlevideo.com/videoplayback"
        "?expire=1788668116&ip=2001-db8-SENTINEL&sig=SIG-SENTINEL-VALUE"
    )
    injected = base_options()["logger"]

    with caplog.at_level(logging.DEBUG, logger="video_downloader.providers.youtube"):
        injected.debug(f'[debug] Invoking http downloader on "{signed}"')
        injected.warning(f"something about {signed}")
        injected.error(f"failed on {signed}")

    rendered = caplog.text
    assert "SIG-SENTINEL-VALUE" not in rendered
    assert "2001-db8-SENTINEL" not in rendered
    assert "1788668116" not in rendered
    assert "/videoplayback" in rendered, "the path is the diagnostic value"


def test_an_unsigned_url_survives_the_logger_intact(caplog):
    plain = "https://media.test/videoplayback/140.m4a"
    injected = base_options()["logger"]

    with caplog.at_level(logging.DEBUG, logger="video_downloader.providers.youtube"):
        injected.debug(f"fetching {plain}")

    assert plain in caplog.text
