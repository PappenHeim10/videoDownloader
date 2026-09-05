"""Which source a requested quality picks, and why.

Selection is pure: a `Media` in, a `MediaSource` out, no request in between. So
every rule here is pinned against constructed sources rather than against a
provider, which is the point - the rules are about the metadata a source
carries, not about who produced it.

The one rule worth stating twice: **a string and an integer are not the same
request.** `"1080p"` is a name and is matched against the provider's own label;
`1080` is a tier and is matched against the provider's own number. On a
portrait video those point at the same file from two different directions, and
on a provider whose labels disagree with its tiers they can point at different
files entirely. Nothing coerces one spelling into the other, so the answer never
depends on how a caller happened to type its argument.
"""

from __future__ import annotations

import pytest
from base_api.models import Media, MediaSource
from base_api.modules.config import DownloadConfigHLS, DownloadConfigHTTP
from base_api.modules.errors import UnsupportedProtocolError

from video_downloader.application.download_service import (
    build_download_config,
    select_progressive_source,
    select_source,
)


def http(
    name: str,
    *,
    value: int | None = None,
    label: str | None = None,
    size: int | None = None,
) -> MediaSource:
    return MediaSource(
        url=f"https://cdn.example/{name}.mp4",
        source_type="HTTP",
        expected_size=size,
        quality_value=value,
        quality_label=label,
    )


def hls(name: str = "master") -> MediaSource:
    return MediaSource(url=f"https://cdn.example/{name}.m3u8", source_type="HLS")


def media(*sources: MediaSource) -> Media:
    return Media(
        provider="test",
        original_url="https://example.test/watch",
        title="A title",
        sources=list(sources),
    )


LADDER = [
    http("480", value=480, label="480p", size=100),
    http("720", value=720, label="720p", size=200),
    http("1080", value=1080, label="1080p", size=400),
]


# --- which transport ------------------------------------------------------


def test_an_hls_only_media_selects_its_playlist():
    playlist = hls()
    assert select_source(media(playlist), "best") is playlist


def test_a_progressive_only_media_selects_a_file():
    chosen = select_source(media(*LADDER), "best")
    assert chosen.source_type == "HTTP"


def test_hls_wins_whenever_a_playlist_exists():
    """A provider offering both publishes the MP4 as a convenience; the
    playlist is what its own player uses, and what has per-segment retries."""
    playlist = hls()
    assert select_source(media(*LADDER, playlist), "1080p") is playlist
    assert select_source(media(playlist, *LADDER), "best") is playlist


@pytest.mark.parametrize("quality", ["best", "worst", "half", "720p", 720])
def test_hls_wins_for_every_requested_quality(quality):
    playlist = hls()
    assert select_source(media(playlist, *LADDER), quality) is playlist


def test_the_playlists_own_quality_is_left_to_the_engine():
    """Nothing here narrows an HLS source: the master playlist has the tiers."""
    playlist = hls()
    config = build_download_config(
        select_source(media(playlist), "720p"),
        quality="720p",
        path="out.mp4",
        callback=lambda done, total: None,
        stop_event=None,
        state_path="state.json",
        remux=True,
    )
    assert isinstance(config, DownloadConfigHLS)
    assert config.quality == "720p"


@pytest.mark.parametrize("source_type", ["DASH", "RTMP", "TORRENT", "", "http"])
def test_an_unknown_transport_stays_unsupported(source_type):
    with pytest.raises(UnsupportedProtocolError):
        select_source(media(MediaSource(url="x://y", source_type=source_type)), "best")


def test_a_media_without_any_source_is_unsupported():
    with pytest.raises(UnsupportedProtocolError):
        select_source(media(), "best")


def test_the_error_names_what_was_offered():
    with pytest.raises(UnsupportedProtocolError) as raised:
        select_source(media(MediaSource(url="x://y", source_type="DASH")), "best")
    assert "DASH" in str(raised.value)


# --- ordering: best / worst / half ----------------------------------------


def test_best_worst_and_half_use_the_numeric_tier():
    assert select_progressive_source(LADDER, "best").quality_value == 1080
    assert select_progressive_source(LADDER, "worst").quality_value == 480
    assert select_progressive_source(LADDER, "half").quality_value == 720


@pytest.mark.parametrize("spelling", ["best", "BEST", " Best ", "bEsT"])
def test_a_preference_is_case_and_whitespace_insensitive(spelling):
    assert select_progressive_source(LADDER, spelling).quality_value == 1080


def test_ordering_does_not_depend_on_the_order_the_provider_listed_them():
    shuffled = [LADDER[2], LADDER[0], LADDER[1]]
    assert select_progressive_source(shuffled, "best").quality_value == 1080
    assert select_progressive_source(shuffled, "worst").quality_value == 480


def test_a_portrait_video_is_ranked_by_its_tier_not_by_its_label():
    """`best` sees 1920, whatever the instance calls it."""
    portrait = http("portrait", value=1920, label="1080p", size=500)
    assert select_progressive_source([*LADDER, portrait], "best") is portrait


def test_the_larger_file_wins_a_tie_on_the_tier():
    small = http("a", value=720, label="720p", size=100)
    large = http("b", value=720, label="720p", size=900)
    assert select_progressive_source([small, large], "best") is large
    assert select_progressive_source([small, large], "worst") is small


def test_a_source_without_a_tier_never_outranks_one_that_has_it():
    unknown = http("unknown", size=10_000_000)
    assert select_progressive_source([*LADDER, unknown], "best").quality_value == 1080
    # ... and among themselves, size is what is left to order by.
    smaller, bigger = http("s", size=10), http("b", size=20)
    assert select_progressive_source([smaller, bigger], "best") is bigger
    assert select_progressive_source([smaller, bigger], "worst") is smaller


def test_selection_is_reproducible_when_everything_ties():
    first = MediaSource(url="https://cdn.example/a.mp4", source_type="HTTP")
    second = MediaSource(url="https://cdn.example/b.mp4", source_type="HTTP")
    assert select_progressive_source([first, second], "best") is second
    assert select_progressive_source([second, first], "best") is second


# --- a concrete quality: strings ------------------------------------------


def test_a_string_matches_the_providers_own_label_first():
    assert select_progressive_source(LADDER, "720p").quality_value == 720


@pytest.mark.parametrize("spelling", ["1080P", " 1080p ", "1080p"])
def test_a_label_match_normalizes_only_case_and_surrounding_space(spelling):
    assert select_progressive_source(LADDER, spelling).quality_value == 1080


def test_the_portrait_case_the_label_match_exists_for():
    """`resolution.id = 1920`, `label = "1080p"`: asking for "1080p" finds it.

    The label is the provider's word for this rendition. Matching it is the
    only way to reach a file whose number says something else.
    """
    portrait = http("portrait", value=1920, label="1080p", size=500)
    ladder = [http("480", value=480, label="480p", size=100), portrait]

    assert select_progressive_source(ladder, "1080p") is portrait
    # The same list, ranked numerically, puts it at the top for a different
    # reason - its tier, not its name.
    assert select_progressive_source(ladder, "best") is portrait


def test_the_same_portrait_video_is_not_found_by_the_integer_1080():
    """A tier of 1920 is not a tier of 1080, and is never treated as one."""
    portrait = http("portrait", value=1920, label="1080p", size=500)
    smaller = http("480", value=480, label="480p", size=100)

    # No exact tier, no smaller one than 1080 except 480 -> the next smaller.
    assert select_progressive_source([smaller, portrait], 1080) is smaller


def test_a_string_falls_through_to_the_numeric_rule_when_no_label_matches():
    unlabelled = [
        http("480", value=480, size=100),
        http("720", value=720, size=200),
    ]
    assert select_progressive_source(unlabelled, "720p").quality_value == 720


def test_a_string_that_is_neither_a_label_nor_a_number_takes_the_best():
    assert select_progressive_source(LADDER, "Original").quality_value == 1080


def test_a_label_tie_is_broken_the_same_way_the_ordering_is():
    small = http("a", value=720, label="720p", size=100)
    large = http("b", value=720, label="720p", size=900)
    assert select_progressive_source([small, large], "720p") is large


# --- a concrete quality: numbers ------------------------------------------


@pytest.mark.parametrize("quality", [720, "720", "720p"])
def test_an_exact_tier_is_taken_whichever_way_it_is_spelled(quality):
    assert select_progressive_source(LADDER, quality).quality_value == 720


@pytest.mark.parametrize("quality", [1440, "1440", "1440p"])
def test_an_unavailable_tier_falls_to_the_next_smaller_one(quality):
    """Downwards: a request for more than exists must not become 2160."""
    assert select_progressive_source(LADDER, quality).quality_value == 1080


@pytest.mark.parametrize("quality", [144, "144", "144p"])
def test_a_tier_below_everything_available_takes_the_smallest(quality):
    assert select_progressive_source(LADDER, quality).quality_value == 480


def test_a_numeric_request_prefers_a_stated_tier_over_an_unstated_one():
    unknown = http("unknown", size=10_000_000)
    assert select_progressive_source([unknown, *LADDER], 240).quality_value == 480


def test_a_numeric_request_against_sources_with_no_tier_at_all_takes_the_smallest():
    sources = [http("a", size=10), http("b", size=20)]
    assert select_progressive_source(sources, 1080) is sources[0]


def test_an_empty_candidate_list_is_unsupported():
    with pytest.raises(UnsupportedProtocolError):
        select_progressive_source([], "best")


# --- the configuration the choice produces --------------------------------


def test_a_progressive_source_produces_an_http_configuration():
    source = http("720", value=720, label="720p", size=1234)
    config = build_download_config(
        source,
        quality="720p",
        path="out.mp4",
        callback=lambda done, total: None,
        stop_event=None,
        state_path="state.json",
        remux=True,
    )

    assert isinstance(config, DownloadConfigHTTP)
    assert config.media_source is source
    # The provider's stated size arrives at the transport that can use it.
    assert config.expected_size == 1234
    assert config.state_path == "state.json"
    assert not hasattr(config, "remux")


def test_a_progressive_source_without_a_stated_size_says_so():
    config = build_download_config(
        http("720", value=720),
        quality="best",
        path="out.mp4",
        callback=lambda done, total: None,
        stop_event=None,
        state_path="state.json",
        remux=True,
    )
    assert config.expected_size is None


def test_an_hls_source_produces_an_hls_configuration_with_its_remux_flag():
    source = hls()
    config = build_download_config(
        source,
        quality="best",
        path="out.mp4",
        callback=lambda done, total: None,
        stop_event=None,
        state_path="state.json",
        remux=False,
    )

    assert isinstance(config, DownloadConfigHLS)
    assert config.media_source is source
    assert config.remux is False
    assert config.segment_state_path == "state.json"


def test_both_transports_write_their_state_beside_the_job():
    """One `.state/` directory, two schemas - the file path is the same one."""
    for source in (hls(), http("720", value=720)):
        config = build_download_config(
            source,
            quality="best",
            path="out.mp4",
            callback=lambda done, total: None,
            stop_event=None,
            state_path="/jobs/.state/abc.json",
            remux=True,
        )
        recorded = getattr(config, "segment_state_path", None) or getattr(
            config, "state_path", None
        )
        assert recorded == "/jobs/.state/abc.json"
