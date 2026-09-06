"""Which tracks a job downloads, decided from a `Media` alone.

No network, no provider names: everything here is built from `MediaSource` and
`MediaTrackInfo`, which is the whole point of putting the rule at this layer.

The cases that matter are the ones where a plausible shortcut gives the wrong
answer - a portrait video whose height contradicts its label, forty-five audio
renditions of which one is the original, a codec preference that cannot be
satisfied above 1080p - so those get the most attention.
"""

from __future__ import annotations

import pytest
from base_api.models import MediaSource, MediaTrackInfo
from base_api.modules.errors import UnsupportedProtocolError

from video_downloader.application.track_selection import (
    TrackSelection,
    codec_family,
    select_tracks,
)


def video(
    tier: int,
    codec: str,
    *,
    label: str | None = None,
    fps: float | None = 30.0,
    container: str = "mp4",
    size: int | None = None,
) -> MediaSource:
    return MediaSource(
        url=f"https://cdn.test/v-{tier}-{codec}-{int(fps or 0)}.{container}",
        source_type="HTTP",
        quality_value=tier,
        quality_label=label if label is not None else f"{tier}p",
        expected_size=size,
        track=MediaTrackInfo(
            role="video", container=container, video_codec=codec, fps=fps,
            height=tier, width=tier * 16 // 9,
        ),
    )


def audio(
    codec: str,
    *,
    bitrate: int | None = 128_000,
    container: str = "mp4",
    default: bool | None = None,
    drc: bool | None = None,
    language: str | None = None,
) -> MediaSource:
    return MediaSource(
        url=f"https://cdn.test/a-{codec}-{bitrate}-{language}-{drc}.{container}",
        source_type="HTTP",
        track=MediaTrackInfo(
            role="audio", container=container, audio_codec=codec,
            bitrate_bps=bitrate, is_default_audio=default,
            is_dynamic_range_compressed=drc, language=language,
        ),
    )


def combined(tier: int, *, label: str | None = None) -> MediaSource:
    return MediaSource(
        url=f"https://cdn.test/c-{tier}.mp4",
        source_type="HTTP",
        quality_value=tier,
        quality_label=label if label is not None else f"{tier}p",
        track=MediaTrackInfo(role="combined", container="mp4"),
    )


# --- codec families --------------------------------------------------------


@pytest.mark.parametrize(
    ("codec", "family"),
    [
        ("avc1.640028", "avc1"),
        ("avc1.4d401f", "avc1"),
        ("vp09.00.40.08", "vp09"),
        ("vp9", "vp09"),
        ("av01.0.08M.08", "av01"),
        ("mp4a.40.2", "mp4a"),
        ("opus", "opus"),
    ],
)
def test_a_codec_string_maps_to_its_family(codec, family):
    assert codec_family(codec) == family


@pytest.mark.parametrize("codec", [None, "", "something-new", "h264"])
def test_an_unrecognised_codec_has_no_family_rather_than_a_guessed_one(codec):
    """A guessed family picks a container the track cannot go into."""
    assert codec_family(codec) is None


def test_the_engine_owns_no_codec_taxonomy():
    """The families live here; the model only stores what a provider wrote.

    Checked over the parsed constants rather than the raw text, because the
    model's docstring names `avc1` on purpose - to say it does *not* normalise
    to it. What must not exist is a codec family as a value the code acts on.
    """
    import ast
    import inspect

    import base_api.models as models

    constants = {
        node.value
        for node in ast.walk(ast.parse(inspect.getsource(models)))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    for family in ("avc1", "vp09", "av01", "mp4a", "opus", "h264"):
        assert family not in constants, f"{family} became a value the engine acts on"


# --- a combined source wins ------------------------------------------------


def test_a_combined_source_is_preferred_over_separate_tracks():
    """It needs no muxing, so it cannot fail at a step the pair has."""
    selection = select_tracks(
        [combined(720), video(1080, "avc1.640028"), audio("mp4a.40.2")], "best"
    )

    assert selection.combined is not None
    assert selection.needs_muxing is False
    assert selection.sources == (selection.combined,)


def test_a_source_that_states_no_role_counts_as_a_finished_file():
    """Every provider that predates roles means "a whole video"; pairing is opt-in."""
    plain = MediaSource(url="https://cdn.test/a.mp4", source_type="HTTP", quality_value=720)

    selection = select_tracks([plain], "best")

    assert selection.combined is plain
    assert selection.needs_muxing is False


# --- pairing ---------------------------------------------------------------


def test_separate_tracks_are_paired_and_marked_as_needing_a_mux():
    selection = select_tracks([video(1080, "avc1.640028"), audio("mp4a.40.2")], "best")

    assert selection.needs_muxing is True
    assert selection.video is not None and selection.audio is not None
    assert selection.sources == (selection.video, selection.audio)


def test_a_video_without_any_audio_track_is_still_downloadable():
    """A silent video is a video."""
    selection = select_tracks([video(720, "avc1.640028")], "best")

    assert selection.needs_muxing is False
    assert selection.combined is not None


def test_audio_only_media_is_refused():
    with pytest.raises(UnsupportedProtocolError):
        select_tracks([audio("mp4a.40.2")], "best")


def test_an_empty_media_is_refused():
    with pytest.raises(UnsupportedProtocolError):
        select_tracks([], "best")


# --- what the user asked for -----------------------------------------------


def test_best_takes_the_highest_tier_even_though_it_needs_muxing():
    ladder = [video(t, "avc1.640028") for t in (360, 720, 1080)]

    selection = select_tracks([*ladder, audio("mp4a.40.2")], "best")

    assert selection.video.quality_value == 1080


def test_worst_and_half_name_positions_in_the_ladder():
    ladder = [video(t, "avc1.640028") for t in (360, 720, 1080)]
    pool = [*ladder, audio("mp4a.40.2")]

    assert select_tracks(pool, "worst").video.quality_value == 360
    assert select_tracks(pool, "half").video.quality_value == 720


def test_an_integer_selects_a_tier_and_falls_downwards():
    ladder = [video(t, "avc1.640028") for t in (360, 720, 1080)]
    pool = [*ladder, audio("mp4a.40.2")]

    assert select_tracks(pool, 720).video.quality_value == 720
    assert select_tracks(pool, 900).video.quality_value == 720
    assert select_tracks(pool, 144).video.quality_value == 360


def test_a_label_matches_the_providers_own_word_for_it():
    """The portrait case: the label and the tier deliberately disagree."""
    portrait = video(1920, "avc1.640028", label="1080p")
    landscape = video(1440, "avc1.640028", label="1440p")

    selection = select_tracks([portrait, landscape, audio("mp4a.40.2")], "1080p")

    assert selection.video is portrait


def test_height_never_outranks_the_providers_tier():
    """A portrait 1080p is 1080x1920; ranking by height puts it above 1440p."""
    portrait = video(1080, "avc1.640028", label="1080p")
    portrait.track.height = 1920
    better = video(1440, "avc1.640028", label="1440p")

    selection = select_tracks([portrait, better, audio("mp4a.40.2")], "best")

    assert selection.video is better


def test_an_uninterpretable_quality_falls_back_to_the_best_track():
    pool = [video(720, "avc1.640028"), video(1080, "avc1.640028"), audio("mp4a.40.2")]

    assert select_tracks(pool, "schnell").video.quality_value == 1080


# --- codec preference ------------------------------------------------------


def test_h264_wins_at_and_below_1080p():
    pool = [
        video(1080, "vp09.00.40.08", container="webm"),
        video(1080, "av01.0.08M.08"),
        video(1080, "avc1.640028"),
        audio("mp4a.40.2"),
        audio("opus", container="webm"),
    ]

    selection = select_tracks(pool, 1080)

    assert codec_family(selection.video.track.video_codec) == "avc1"
    assert codec_family(selection.audio.track.audio_codec) == "mp4a"


def test_vp9_takes_over_above_1080p_where_no_h264_exists():
    """The preference cannot be satisfied there, so the next one applies."""
    pool = [
        video(2160, "vp09.00.51.08", container="webm"),
        video(2160, "av01.0.13M.08"),
        audio("mp4a.40.2"),
        audio("opus", container="webm"),
    ]

    selection = select_tracks(pool, "best")

    assert codec_family(selection.video.track.video_codec) == "vp09"
    assert codec_family(selection.audio.track.audio_codec) == "opus"


def test_av1_is_chosen_only_when_it_is_the_only_family_left():
    pool = [video(2160, "av01.0.13M.08"), audio("mp4a.40.2")]

    selection = select_tracks(pool, "best")

    assert codec_family(selection.video.track.video_codec) == "av01"


def test_a_higher_frame_rate_wins_among_tracks_of_one_tier():
    pool = [
        video(1080, "avc1.640028", fps=30.0),
        video(1080, "avc1.640028", fps=60.0),
        audio("mp4a.40.2"),
    ]

    assert select_tracks(pool, 1080).video.track.fps == 60.0


# --- audio coupling --------------------------------------------------------


def test_a_dynamic_range_compressed_rendition_is_not_chosen():
    original = audio("mp4a.40.2", bitrate=128_000, drc=False)
    loudness_normalised = audio("mp4a.40.2", bitrate=999_000, drc=True)

    selection = select_tracks(
        [video(720, "avc1.640028"), loudness_normalised, original], "best"
    )

    assert selection.audio is original, "a louder DRC copy must not win on bitrate"


def test_only_the_default_track_is_taken_when_the_provider_marks_one():
    """Forty-five renditions, one original: the flag is the only thing that tells them apart."""
    dubs = [
        audio("mp4a.40.2", bitrate=192_000, default=False, language=code)
        for code in ("de-DE", "es-US", "hi", "ja")
    ]
    original = audio("mp4a.40.2", bitrate=128_000, default=True, language="en-US")

    selection = select_tracks([video(720, "avc1.640028"), *dubs, original], "best")

    assert selection.audio is original
    assert selection.audio.track.language == "en-US"


def test_where_no_track_is_marked_default_the_flag_filters_nothing():
    quiet = audio("mp4a.40.2", bitrate=64_000)
    loud = audio("mp4a.40.2", bitrate=192_000)

    selection = select_tracks([video(720, "avc1.640028"), quiet, loud], "best")

    assert selection.audio is loud


def test_the_highest_bitrate_wins_among_equals():
    low = audio("opus", bitrate=64_000, container="webm")
    high = audio("opus", bitrate=160_000, container="webm")

    selection = select_tracks(
        [video(2160, "vp09.00.51.08", container="webm"), low, high], "best"
    )

    assert selection.audio is high


def test_a_container_mismatch_is_accepted_rather_than_leaving_a_video_silent():
    """No matching codec is a reason to mux into Matroska, not to give up."""
    selection = select_tracks(
        [video(1080, "avc1.640028"), audio("opus", container="webm")], "best"
    )

    assert selection.audio is not None
    assert codec_family(selection.audio.track.audio_codec) == "opus"


def test_every_audio_rendition_being_drc_still_yields_audio():
    """A filter that would empty the list is skipped; some audio beats none."""
    only = audio("mp4a.40.2", drc=True)

    selection = select_tracks([video(720, "avc1.640028"), only], "best")

    assert selection.audio is only


def test_the_selection_is_reproducible_for_one_media():
    pool = [
        video(1080, "avc1.640028"),
        video(1080, "vp09.00.40.08", container="webm"),
        video(720, "avc1.4d401f"),
        audio("mp4a.40.2", default=True),
        audio("opus", container="webm", default=False),
    ]

    first = select_tracks(pool, "best")
    second = select_tracks(list(reversed(pool)), "best")

    assert first.video.url == second.video.url
    assert first.audio.url == second.audio.url


def test_a_selection_names_exactly_one_shape():
    assert TrackSelection(combined=combined(720)).needs_muxing is False
    assert TrackSelection(
        video=video(720, "avc1.640028"), audio=audio("mp4a.40.2")
    ).needs_muxing is True
