"""B8: who decides where a download lands, and when.

The destination is a user decision, persisted once and reused. The download
engine never chooses it - it receives a resolved directory. Everything here runs
against an isolated settings root, so no test reads or writes the real AppData.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from base_api.base import BaseCore
from base_api.models import Media, MediaSource
from xhamster_api.xhamster_api import Video

from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.download_service import run_download_job
from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.infrastructure.paths import HOME_ENV_VAR
from video_downloader.infrastructure.settings import AppSettings
from video_downloader.ui.main_window import MainWindow

PROVIDER_URL = "https://xhamster.com/videos/example-1"


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path, monkeypatch) -> AppSettings:
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    return AppSettings()


def _directory(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    target.mkdir()
    return target


def _window(settings: AppSettings, picker_result: Path | None):
    """A window over a mocked manager: this exercises the GUI's decision, not the engine."""
    manager = MagicMock(spec=DownloadManager)
    manager.get_jobs.return_value = []
    manager.add_download.return_value = DownloadJob(
        url=PROVIDER_URL, quality="best", output_dir=Path(".")
    )

    window = MainWindow(manager, settings)
    calls: list[str] = []

    def picker(title: str):
        calls.append(title)
        return picker_result

    window._ask_for_directory = picker
    window.url.setText(PROVIDER_URL)
    return window, manager, calls


# --- first use --------------------------------------------------------------


def test_first_download_asks_for_a_directory_and_persists_it(qt_app, settings, tmp_path):
    videos = _directory(tmp_path, "videos")
    window, manager, calls = _window(settings, videos)

    window.add_download()

    assert calls, "the picker was never opened"
    assert settings.get_download_directory() == videos.resolve()
    assert manager.add_download.call_args.kwargs["output_dir"] == videos.resolve()


def test_a_configured_directory_is_reused_without_asking(qt_app, settings, tmp_path):
    videos = _directory(tmp_path, "videos")
    settings.set_download_directory(videos)
    window, manager, calls = _window(settings, None)

    window.add_download()

    assert calls == [], "the user was asked again despite a valid setting"
    assert manager.add_download.call_args.kwargs["output_dir"] == videos.resolve()


def test_a_vanished_directory_causes_a_fresh_request(qt_app, settings, tmp_path):
    gone = _directory(tmp_path, "gone")
    settings.set_download_directory(gone)
    gone.rmdir()

    replacement = _directory(tmp_path, "replacement")
    window, manager, calls = _window(settings, replacement)

    window.add_download()

    assert calls, "the invalid setting was used silently"
    assert settings.get_download_directory() == replacement.resolve()
    assert manager.add_download.call_args.kwargs["output_dir"] == replacement.resolve()


# --- cancellation -----------------------------------------------------------


def test_cancelling_the_picker_starts_no_download(qt_app, settings, tmp_path):
    window, manager, calls = _window(settings, None)

    window.add_download()

    assert calls, "the picker should have been opened"
    manager.add_download.assert_not_called()


def test_cancelling_the_picker_stores_no_fallback(qt_app, settings):
    window, _, _ = _window(settings, None)

    window.add_download()

    assert settings.get_download_directory() is None


def test_cancelling_leaves_the_window_usable_and_the_url_intact(qt_app, settings, tmp_path):
    window, manager, _ = _window(settings, None)

    window.add_download()

    # No exception, and the typed URL is still there so a second attempt is cheap.
    assert window.url.text() == PROVIDER_URL

    videos = _directory(tmp_path, "videos")
    window._ask_for_directory = lambda title: videos
    window.add_download()

    assert manager.add_download.call_args.kwargs["output_dir"] == videos.resolve()


# --- changing the directory -------------------------------------------------


def test_changing_the_directory_persists_immediately(qt_app, settings, tmp_path):
    first = _directory(tmp_path, "first")
    second = _directory(tmp_path, "second")
    settings.set_download_directory(first)

    window, _, _ = _window(settings, second)
    window.change_download_directory()

    assert settings.get_download_directory() == second.resolve()


def test_cancelling_a_change_keeps_the_previous_directory(qt_app, settings, tmp_path):
    first = _directory(tmp_path, "first")
    settings.set_download_directory(first)

    window, _, _ = _window(settings, None)
    window.change_download_directory()

    assert settings.get_download_directory() == first.resolve()


# --- the resolved directory reaching a real job -----------------------------


async def _noop_runner(job: DownloadJob) -> DownloadJob:
    return job


@pytest.mark.asyncio
async def test_a_job_is_created_in_the_configured_directory(settings, tmp_path):
    videos = _directory(tmp_path, "videos")
    settings.set_download_directory(videos)

    manager = DownloadManager(job_runner=_noop_runner)
    job = manager.add_download(PROVIDER_URL, output_dir=settings.get_download_directory())

    assert job.output_dir == videos.resolve()


@pytest.mark.asyncio
async def test_a_directory_change_applies_to_future_jobs_only(settings, tmp_path):
    first = _directory(tmp_path, "first")
    second = _directory(tmp_path, "second")

    manager = DownloadManager(job_runner=_noop_runner)

    settings.set_download_directory(first)
    job_a = manager.add_download(PROVIDER_URL, output_dir=settings.get_download_directory())

    settings.set_download_directory(second)
    job_b = manager.add_download(PROVIDER_URL, output_dir=settings.get_download_directory())

    assert job_a.output_dir == first.resolve()
    assert job_b.output_dir == second.resolve()


@pytest.mark.asyncio
async def test_the_manager_refuses_to_invent_a_directory(tmp_path):
    manager = DownloadManager(job_runner=_noop_runner)

    with pytest.raises(ValueError, match="Kein Download-Verzeichnis"):
        manager.add_download(PROVIDER_URL)


# --- the full path: configured directory -> sanitized file -> B1 ------------


@pytest.mark.asyncio
async def test_configured_directory_carries_through_to_the_completed_file(settings, tmp_path):
    videos = _directory(tmp_path, "videos")
    settings.set_download_directory(videos)

    core = MagicMock(spec=BaseCore)
    core.download = AsyncMock(return_value=True)
    core.close = AsyncMock()

    raw_title = "Why: does this work?"
    video = Video(url=PROVIDER_URL, core=core)
    video.__dict__["title"] = raw_title
    media = Media(provider="xHamster", original_url=PROVIDER_URL, title=raw_title)
    media.sources = [MediaSource(url="https://cdn.example/x.m3u8", source_type="HLS")]
    video.to_media = MagicMock(return_value=media)

    client = MagicMock()
    client.core = core
    client.get_video = AsyncMock(return_value=video)

    job = DownloadJob(
        url=PROVIDER_URL, quality="best", output_dir=settings.get_download_directory()
    )
    await run_download_job(job, client_factory=lambda: client)

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file is not None                       # B1 stays fixed
    assert job.output_file.parent == videos.resolve()         # B8: configured directory
    assert job.output_file.name == "Why_ does this work_.mp4"  # sanitizer unchanged
    assert job.title == raw_title                             # display title untouched
