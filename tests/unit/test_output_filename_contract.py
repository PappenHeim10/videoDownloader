"""The path the app records and the file the engine writes must be the same.

The application computes `job.output_file` from the resolved media's title and
hands that very string to the HLS engine. If the recorded path and the written
one could differ, the app would record `raw:title?.mp4` while `raw_title_.mp4`
landed on disk, and opening or deleting the finished download would silently
target a file that never existed.

Before A1 this was a cross-repository agreement: the app derived the name, and
the provider's own `download()` derived it again from the same sanitizer. Now
the app derives it once and passes it down, so the two cannot drift - which is
what these tests pin. The sanitizer itself is deliberately not mocked.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from base_api.base import BaseCore
from base_api.models import Media, MediaSource
from base_api.modules.static_functions import strip_title

from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob, LifecycleState

PROVIDER_URL = "https://xhamster.com/videos/example-1"


def _session_for(title: str):
    """A resolved media with the raw scraped title, over a stubbed engine."""
    core = MagicMock(spec=BaseCore)
    core.download = AsyncMock(return_value=True)
    core.close = AsyncMock()

    media = Media(provider="xHamster", original_url=PROVIDER_URL, title=title)
    media.sources = [MediaSource(url="https://cdn.example/x.m3u8", source_type="HLS")]

    registry = MagicMock()
    registry.resolve = AsyncMock(return_value=media)
    registry.close = AsyncMock()
    return ProviderSession(registry=registry, core=core), core


async def _run(title: str, output_dir: Path):
    session, core = _session_for(title)
    job = DownloadJob(url=PROVIDER_URL, quality="best", output_dir=output_dir)

    await run_download_job(job, session_factory=lambda: session)

    # Read straight off the finished job. This used to need an observer to catch
    # the value mid-flight, because B1 cleared it on successful completion.
    assert job.state == LifecycleState.COMPLETED
    assert job.output_file is not None

    engine_path = Path(core.download.await_args.args[0].path)
    return job.output_file, engine_path, job


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    [
        "Ein ganz normaler Titel",
        "Why: does this work?",
        "star*and|pipe",
        "../outside",
        r"..\outside",
        r"C:\Windows\System32\evil",
        "sub/dir/file",
        "///",
        "漢" * 300,
    ],
)
async def test_app_and_engine_use_the_same_filename(tmp_path, title):
    app_path, engine_path, job = await _run(title, tmp_path)

    assert app_path == engine_path
    # The completed job must still carry that path - the UI opens and deletes by it.
    assert job.state == LifecycleState.COMPLETED
    assert job.output_file == app_path


@pytest.mark.asyncio
async def test_recorded_path_matches_the_canonical_sanitizer(tmp_path):
    app_path, _, _ = await _run("Why: does this work?", tmp_path)
    assert app_path.name == f"{strip_title('Why: does this work?')}.mp4"
    assert app_path.name == "Why_ does this work_.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    ["../outside", r"..\outside", r"C:\Windows\System32\evil", "sub/dir/file", ".."],
)
async def test_a_scraped_title_cannot_move_the_recorded_path_out_of_the_output_dir(tmp_path, title):
    app_path, engine_path, _ = await _run(title, tmp_path)

    for candidate in (app_path, engine_path):
        resolved = candidate.resolve()
        assert resolved.is_relative_to(tmp_path.resolve())
        assert resolved.parent == tmp_path.resolve()


@pytest.mark.asyncio
async def test_display_title_is_kept_human_readable(tmp_path):
    raw = "Why: does this work?"
    app_path, _, job = await _run(raw, tmp_path)

    # The UI reads job.title; it must not be given the escaped filename.
    assert job.title == raw
    assert app_path.name != f"{raw}.mp4"


@pytest.mark.asyncio
async def test_a_title_that_sanitises_to_nothing_never_yields_a_bare_extension(tmp_path):
    app_path, engine_path, _ = await _run("///", tmp_path)
    assert app_path.name == "untitled.mp4"
    assert engine_path.name == "untitled.mp4"


# --- the extension names what is actually in the file ----------------------


def test_an_hls_download_is_still_called_mp4():
    """It is remuxed into MP4 whatever its segments were."""
    from base_api.models import MediaSource

    from video_downloader.application.download_service import output_extension

    assert output_extension(MediaSource(url="https://c/x.m3u8", source_type="HLS")) == ".mp4"


def test_a_progressive_source_is_named_after_its_container():
    from base_api.models import MediaSource, MediaTrackInfo

    from video_downloader.application.download_service import output_extension

    for container, expected in (("mp4", ".mp4"), ("webm", ".webm"), ("mkv", ".mkv")):
        source = MediaSource(
            url=f"https://c/x.{container}",
            source_type="HTTP",
            track=MediaTrackInfo(container=container),
        )
        assert output_extension(source) == expected


def test_a_provider_that_states_no_container_keeps_the_historical_name():
    from base_api.models import MediaSource

    from video_downloader.application.download_service import output_extension

    assert output_extension(MediaSource(url="https://c/x", source_type="HTTP")) == ".mp4"


@pytest.mark.asyncio
async def test_a_webm_source_lands_under_a_webm_name(tmp_path):
    """End to end: the recorded path and the engine's path agree, and both say webm."""
    from base_api.models import MediaSource, MediaTrackInfo

    core = MagicMock(spec=BaseCore)
    core.download = AsyncMock(return_value=True)
    core.close = AsyncMock()

    media = Media(provider="test", original_url=PROVIDER_URL, title="A Video")
    media.sources = [
        MediaSource(
            url="https://cdn.example/v.webm",
            source_type="HTTP",
            quality_value=1080,
            track=MediaTrackInfo(role="combined", container="webm"),
        )
    ]
    registry = MagicMock()
    registry.resolve = AsyncMock(return_value=media)
    registry.close = AsyncMock()
    session = ProviderSession(registry=registry, core=core)

    job = DownloadJob(url=PROVIDER_URL, quality="best", output_dir=tmp_path)
    await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file.suffix == ".webm"
    assert Path(core.download.await_args.args[0].path) == job.output_file
