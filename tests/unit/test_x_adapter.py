"""The X adapter: which URLs it claims, and what it makes of an answer.

Nothing here touches the network. `x_post.json` is a real yt-dlp answer with the
media URLs replaced by synthetic ones and the account anonymised - the parts
that could not go into a repository - and reduced to the fields the adapter
reads. `x_post_hls_only.json` is the same answer with the progressive entries
removed, which is what a post whose video X publishes only as a playlist looks
like.

The cases that carry weight are the ones where the obvious shortcut invents a
fact: X states no codecs on the files it *can* hand over, no quality label at
all, and a size that is derived from a bitrate which does not describe them.
Each has a test that the corresponding field stays unset rather than being
filled in from the extension, the dimensions or arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from base_api.modules.errors import UnsupportedURLError
from base_api.provider import MediaProvider

from video_downloader.application.track_download import YTDLP_TRANSPORT
from video_downloader.providers.x import (
    XAdapter,
    XExtractionError,
    XLiveNotSupportedError,
    XNoSupportedSourceError,
    XUnavailableError,
    XUnsupportedTargetError,
    _canonical_post_id,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
POST_URL = "https://x.com/example_poster/status/2096518350553940450"
POST_ID = "2096518350553940450"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def adapter_for(name: str) -> XAdapter:
    payload = load(name)
    return XAdapter(resolver=lambda url: payload)


def adapter_raising(error: Exception) -> XAdapter:
    def resolver(url: str):
        raise error

    return XAdapter(resolver=resolver)


# --- contract --------------------------------------------------------------


def test_the_adapter_satisfies_the_provider_contract():
    assert isinstance(XAdapter(), MediaProvider)


# --- which URLs are claimed -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/example_poster/status/2096518350553940450",
        "https://www.x.com/example_poster/status/2096518350553940450",
        "https://mobile.x.com/example_poster/status/2096518350553940450",
        "https://twitter.com/example_poster/status/2096518350553940450",
        "https://www.twitter.com/example_poster/status/2096518350553940450",
        "https://mobile.twitter.com/example_poster/status/2096518350553940450",
        "https://x.com/example_poster/statuses/2096518350553940450",
        "https://x.com/example_poster/status/2096518350553940450/video/1",
        "https://x.com/example_poster/status/2096518350553940450/photo/1",
        "https://x.com/i/status/2096518350553940450",
        "https://x.com/i/web/status/2096518350553940450",
        "https://x.com/example_poster/status/2096518350553940450?s=20&t=abc",
    ],
)
def test_every_form_of_a_post_link_yields_the_same_id(url):
    """Both hosts, both spellings, the internal namespace, and the query X adds
    when a post is shared from the app - one post, one id."""
    assert _canonical_post_id(url) == POST_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=tEwb4cuFjKE",
        "https://example.com/example_poster/status/2096518350553940450",
        "http://xx.com/a/status/1",
        "ftp://x.com/example_poster/status/2096518350553940450",
        "https://x.com/example_poster/status/not-a-number",
        "https://x.com/example_poster/status/2096518350553940450/analytics",
    ],
)
def test_a_url_that_is_not_a_post_is_not_claimed(url):
    assert XAdapter().supports(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/example_poster",
        "https://x.com/example_poster/with_replies",
        "https://x.com/example_poster/likes",
        "https://x.com/home",
        "https://x.com/search?q=cats",
        "https://x.com/explore",
        "https://x.com/hashtag/cats",
        "https://x.com/i/lists/12345",
        "https://x.com/i/anything-else",
        "https://x.com/",
    ],
)
def test_a_profile_or_feed_is_claimed_so_the_refusal_can_say_why(url):
    """Answering "unsupported URL" to a profile link tells the user nothing.

    The adapter claims it, and `resolve` then explains what the link is.
    """
    assert XAdapter().supports(url) is True
    with pytest.raises(XUnsupportedTargetError):
        _canonical_post_id(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/i/spaces/1YpKkZQwlbBxj",
        "https://twitter.com/i/spaces/1YpKkZQwlbBxj",
        "https://x.com/i/broadcasts/1abcdEFGH2345",
    ],
)
def test_a_space_is_refused_as_live_rather_than_as_a_missing_post(url):
    """`/i/spaces/<id>` is the link someone pastes *for* a Space.

    It names one directly and never resolves to a post, so the live check that
    runs after a resolution can never see it - which left the one sentence that
    fits ("live is not supported") reachable only for a post that happens to be
    live, and gave a Space the generic "this names no single post" instead.
    """
    assert XAdapter().supports(url) is True
    with pytest.raises(XLiveNotSupportedError):
        _canonical_post_id(url)


@pytest.mark.asyncio
async def test_a_space_is_refused_before_anything_is_resolved():
    with pytest.raises(XLiveNotSupportedError):
        await adapter_raising(AssertionError("must not resolve")).resolve(
            "https://x.com/i/spaces/1YpKkZQwlbBxj"
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/",
        "https://x.com/example_poster",
        "https://x.com/home",
        "https://x.com/i/spaces/1YpKkZQwlbBxj",
        "https://x.com/i/broadcasts/1abcdEFGH2345",
        "https://x.com/i/anything-else",
        "https://x.com/example_poster/status/2096518350553940450",
        "https://example.com/not-ours",
    ],
)
def test_supports_answers_rather_than_raising(url):
    """The registry asks every adapter about every URL a user pastes.

    An exception escaping here would fail a link that belongs to some other
    adapter entirely, so every refusal this adapter can name has to come back
    as "yes, mine" and be explained by `resolve`.
    """
    assert XAdapter().supports(url) in (True, False)


def test_supports_never_reaches_the_network(monkeypatch):
    """It is called for every registered adapter on every URL a user pastes."""

    def explode(*args, **kwargs):
        raise AssertionError("supports() must decide from the URL alone")

    monkeypatch.setattr(
        "video_downloader.providers.x.XAdapter._extract", explode
    )
    assert XAdapter().supports(POST_URL) is True


# --- what an answer becomes -------------------------------------------------


@pytest.mark.asyncio
async def test_a_post_resolves_into_the_progressive_files_only():
    media = await adapter_for("x_post.json").resolve(POST_URL)

    assert media.provider == "x"
    assert media.provider_id == POST_ID
    assert [source.quality_value for source in media.sources] == [320, 540, 720, 1080]
    assert all(source.source_type == YTDLP_TRANSPORT for source in media.sources)


@pytest.mark.asyncio
async def test_the_playlist_renditions_are_never_offered_as_files():
    """X publishes the same video twice; only one of the two is one file."""
    media = await adapter_for("x_post.json").resolve(POST_URL)

    identities = [source.identity for source in media.sources]
    assert all("hls" not in identity for identity in identities), identities
    assert identities == [f"x:{POST_ID}:http-{tbr}" for tbr in (432, 832, 1280, 8768)]


@pytest.mark.asyncio
async def test_a_source_states_no_role_because_x_states_no_codecs():
    """The whole reason there is no muxing here.

    yt-dlp reports neither `vcodec` nor `acodec` for these formats, so the
    adapter has nothing to base a role on. Inferring "combined" from the `.mp4`
    extension would be this adapter stating a fact X did not - and it buys
    nothing, because a source that states no role is already read as one
    finished file.
    """
    media = await adapter_for("x_post.json").resolve(POST_URL)

    for source in media.sources:
        assert source.track.role is None
        assert source.track.video_codec is None
        assert source.track.audio_codec is None


@pytest.mark.asyncio
async def test_the_tier_is_the_short_side_and_no_label_is_invented():
    """X publishes no `format_note`, so there is no provider word to keep."""
    media = await adapter_for("x_post.json").resolve(POST_URL)

    for source in media.sources:
        assert source.quality_label is None
        assert source.quality_value == min(source.track.width, source.track.height)


@pytest.mark.asyncio
async def test_no_size_is_published_because_the_one_x_derives_is_wrong():
    """The fixture states `filesize_approx` on every format, and none is used.

    yt-dlp derives it from the bitrate, and X's bitrates do not describe its
    files. Measured against the real `Content-Range` totals on 2026-09-07, the
    four formats of this very post were overstated by 2.81x, 2.69x, 2.52x and
    5.70x - the largest claiming 13.6 MB for a file of 2.39 MB.

    Carrying that number would put a wrong total on the progress bar, ask about
    a two-gigabyte download that is four hundred megabytes, and send the
    readability probe after a byte past the end of the file.
    """
    payload = load("x_post.json")
    stated = [entry.get("filesize_approx") for entry in payload["formats"]]
    assert any(stated), "the fixture would not test anything otherwise"

    media = await adapter_for("x_post.json").resolve(POST_URL)

    assert [source.expected_size for source in media.sources] == [None] * 4


@pytest.mark.asyncio
async def test_the_container_comes_from_the_extension_the_resolver_reported():
    media = await adapter_for("x_post.json").resolve(POST_URL)

    assert {source.track.container for source in media.sources} == {"mp4"}


@pytest.mark.asyncio
async def test_the_bitrate_is_converted_from_kbit_to_bits():
    """`tbr` is kbit/s as a float; the model is documented in bits per second."""
    media = await adapter_for("x_post.json").resolve(POST_URL)

    assert [source.track.bitrate_bps for source in media.sources] == [
        432_000, 832_000, 1_280_000, 8_768_000
    ]


@pytest.mark.asyncio
async def test_the_post_metadata_survives_into_the_media():
    media = await adapter_for("x_post.json").resolve(POST_URL)

    assert media.authors == ["Example Poster"]
    # 12.422 seconds, stated fractional and carried as whole seconds.
    assert media.duration == 12


# --- what a post is called --------------------------------------------------


@pytest.mark.asyncio
async def test_a_title_that_is_only_a_shortened_link_becomes_a_readable_name():
    """The measured case, and the reason this rule exists at all.

    yt-dlp titles the post "Example Poster - https://t.co/6zAai0et4b". The
    filename sanitiser keeps only the last path component of what it is handed,
    so that title arrives on disk as `6zAai0et4b.mp4` - named after a shortener
    token, with the poster gone.
    """
    media = await adapter_for("x_post.json").resolve(POST_URL)

    assert media.title == f"Example Poster - {POST_ID}"


@pytest.mark.asyncio
async def test_a_post_with_something_to_say_keeps_what_it_said():
    payload = load("x_post.json")
    payload["title"] = "Example Poster - a heron eating a whole fish"

    media = await XAdapter(resolver=lambda url: payload).resolve(POST_URL)

    assert media.title == "Example Poster - a heron eating a whole fish"


@pytest.mark.asyncio
async def test_the_link_goes_but_the_words_around_it_stay():
    payload = load("x_post.json")
    payload["title"] = "Example Poster - look at this https://t.co/6zAai0et4b"

    media = await XAdapter(resolver=lambda url: payload).resolve(POST_URL)

    assert media.title == "Example Poster - look at this"


@pytest.mark.asyncio
async def test_two_wordless_posts_by_one_account_do_not_share_a_name():
    """Falling back to the poster's name alone would overwrite yesterday's file."""
    payload = load("x_post.json")
    payload["title"] = "Example Poster - https://t.co/aaaaaaaaaa"
    other = "https://x.com/example_poster/status/2096518350553940451"

    first = await XAdapter(resolver=lambda url: payload).resolve(POST_URL)
    second = await XAdapter(resolver=lambda url: payload).resolve(other)

    assert first.title != second.title


@pytest.mark.asyncio
async def test_a_post_with_no_title_at_all_is_still_named():
    payload = load("x_post.json")
    payload["title"] = None

    media = await XAdapter(resolver=lambda url: payload).resolve(POST_URL)

    assert media.title == f"Example Poster - {POST_ID}"


@pytest.mark.asyncio
async def test_the_identity_is_the_url_id_not_the_id_x_answered_with():
    """X answers with the id of the tweet carrying the media, which for a quote
    or a retweet is not the one in the link.

    The identity has to name what a re-resolution will be asked for, and a
    re-resolution is asked for the page the user pasted.
    """
    payload = load("x_post.json")
    assert payload["id"] != POST_ID, "the fixture would not test anything otherwise"

    media = await XAdapter(resolver=lambda url: payload).resolve(POST_URL)

    assert all(source.identity.startswith(f"x:{POST_ID}:") for source in media.sources)


@pytest.mark.asyncio
async def test_headers_are_carried_per_source_and_carry_no_cookie():
    media = await adapter_for("x_post.json").resolve(POST_URL)

    for source in media.sources:
        assert "User-Agent" in source.headers
        assert not any(name.lower() == "cookie" for name in source.headers)


@pytest.mark.asyncio
async def test_each_source_owns_its_headers():
    """Two sources sharing one dictionary means one job's edit reaches another."""
    media = await adapter_for("x_post.json").resolve(POST_URL)

    first, second = media.sources[0], media.sources[1]
    first.headers["Referer"] = "https://x.com/"

    assert "Referer" not in second.headers


# --- failure ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unsupported_url_is_refused_before_anything_is_resolved():
    with pytest.raises(UnsupportedURLError):
        await adapter_raising(AssertionError("must not resolve")).resolve(
            "https://example.com/whatever"
        )


@pytest.mark.asyncio
async def test_a_profile_url_is_refused_with_its_own_sentence():
    with pytest.raises(XUnsupportedTargetError):
        await adapter_raising(AssertionError("must not resolve")).resolve(
            "https://x.com/example_poster"
        )


@pytest.mark.asyncio
async def test_a_post_with_only_playlist_renditions_is_reported_as_such():
    with pytest.raises(XNoSupportedSourceError):
        await adapter_for("x_post_hls_only.json").resolve(POST_URL)


@pytest.mark.asyncio
async def test_a_live_broadcast_is_refused_without_a_media_request():
    payload = load("x_post.json")
    payload["is_live"] = True

    with pytest.raises(XLiveNotSupportedError):
        await XAdapter(resolver=lambda url: payload).resolve(POST_URL)


@pytest.mark.asyncio
async def test_an_answer_that_is_not_a_post_description_is_an_extraction_error():
    """yt-dlp returns `None` rather than raising for some failures."""
    with pytest.raises(XExtractionError):
        await XAdapter(resolver=lambda url: None).resolve(POST_URL)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("No video could be found in this tweet", XNoSupportedSourceError),
        ("This account is protected", XUnavailableError),
        ("Requested tweet is private", XUnavailableError),
        ("Account has been suspended", XUnavailableError),
        ("Rate limit exceeded", XUnavailableError),
        ("HTTP Error 429: Too Many Requests", XUnavailableError),
        ("NSFW tweet requires authentication", XUnavailableError),
        ("You have to log in to view this", XUnavailableError),
        ("Tweet not found", XExtractionError),
        ("This tweet was deleted", XExtractionError),
        ("Something entirely new went wrong", XExtractionError),
    ],
)
def test_a_resolver_message_becomes_the_failure_this_application_names(message, expected):
    assert isinstance(XAdapter._classify(message, RuntimeError(message)), expected)


def test_an_unrecognised_message_is_never_softened_into_a_friendlier_one():
    """Guessing turns "we do not know" into a sentence the user will act on."""
    failure = XAdapter._classify("wat", RuntimeError("wat"))

    assert type(failure) is XExtractionError
    assert "wat" in str(failure)
