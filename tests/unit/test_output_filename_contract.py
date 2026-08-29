"""The path the app records and the file the provider writes must be the same.

This is a cross-repository contract. The application computes `job.output_file`
itself, while the actual filename is built inside the provider's `download()`.
If the two applied different rules, the app would record `raw:title?.mp4` while
`raw_title_.mp4` landed on disk, and opening or deleting the finished download
would silently target a file that never existed.

The sanitizer is deliberately not mocked here - the point is to exercise the real
one on both sides.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from base_api.base import BaseCore
from base_api.models import Media, MediaSource
from base_api.modules.static_functions import strip_title
from xhamster_api.xhamster_api import Video

from video_downloader.application.download_service import run_download_job
from video_downloader.domain.download_job import DownloadJob, LifecycleState

PROVIDER_URL = "https://xhamster.com/videos/example-1"


def _client_for(title: str):
    """A real Video over a stubbed core, so the provider's own path logic runs."""
    core = MagicMock(spec=BaseCore)
    core.download = AsyncMock(return_value=True)
    core.close = AsyncMock()

    video = Video(url=PROVIDER_URL, core=core)
    video.__dict__["title"] = title  # seed the cached_property, skip extraction
    media = Media(provider="xHamster", original_url=PROVIDER_URL, title=title)
    media.sources = [MediaSource(url="https://cdn.example/x.m3u8", source_type="HLS")]
    video.to_media = MagicMock(return_value=media)

    client = MagicMock()
    client.core = core
    client.get_video = AsyncMock(return_value=video)
    return client, core


async def _run(title: str, output_dir: Path):
    client, core = _client_for(title)
    job = DownloadJob(url=PROVIDER_URL, quality="best", output_dir=output_dir)

    await run_download_job(job, client_factory=lambda: client)

    # Read straight off the finished job. This used to need an observer to catch
    # the value mid-flight, because B1 cleared it on successful completion.
    assert job.state == LifecycleState.COMPLETED
    assert job.output_file is not None

    provider_path = Path(core.download.await_args.args[0].path)
    return job.output_file, provider_path, job


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
async def test_app_and_provider_derive_the_same_filename(tmp_path, title):
    app_path, provider_path, job = await _run(title, tmp_path)

    assert app_path.name == provider_path.name
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
    app_path, provider_path, _ = await _run(title, tmp_path)

    for candidate in (app_path, provider_path):
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
    app_path, provider_path, _ = await _run("///", tmp_path)
    assert app_path.name == "untitled.mp4"
    assert provider_path.name == "untitled.mp4"
