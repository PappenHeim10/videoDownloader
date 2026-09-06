"""Asking before a download large enough that the answer still matters.

A 2160p60 track measured 1.4 GB, so "one click and a gigabyte" is a real
sequence rather than a hypothetical one. The question has to be asked before the
first byte - afterwards there is nothing left to save - and only a layer with a
window can ask it, which is why the window supplies the answer and the job layer
merely honours it.

The dialog itself is never opened here. `QMessageBox.question` is replaced, as
the directory picker already is in the sibling tests, because what is worth
pinning is when it is asked, what it is asked about, and what happens to the
job either way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.application.track_download import LARGE_DOWNLOAD_BYTES
from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.infrastructure.settings import AppSettings
from video_downloader.ui.main_window import MainWindow

from base_api.base import BaseCore
from base_api.models import Media, MediaSource, MediaTrackInfo


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_DOWNLOADER_HOME", str(tmp_path / "appdata"))
    videos = tmp_path / "videos"
    videos.mkdir()
    settings = AppSettings()
    settings.set_download_directory(videos)

    manager = MagicMock(spec=DownloadManager)
    manager.get_jobs.return_value = []
    manager.add_download.return_value = DownloadJob(
        url="https://provider.test/x", quality="best", output_dir=videos
    )
    return MainWindow(manager, settings)


# --- what the window asks --------------------------------------------------


def test_the_window_hands_its_own_confirmer_to_every_job(window):
    window.url.setText("https://provider.test/x")
    window.add_download()

    kwargs = window.manager.add_download.call_args.kwargs
    assert kwargs["confirm_large_download"] == window.confirm_large_download


def test_a_confirmed_download_is_allowed_to_start(window, monkeypatch, tmp_path):
    asked: list[str] = []

    def question(parent, title, text, *args):
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)
    job.title = "A Very Large Video"

    assert window.confirm_large_download(job, 3 * 1024**3) is True
    assert len(asked) == 1
    assert "3.0 GiB" in asked[0], "the size is the whole point of the question"
    assert "A Very Large Video" in asked[0], "and so is which video it is about"


def test_a_refused_download_says_so_in_the_status_bar(window, monkeypatch, tmp_path):
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *args: QMessageBox.StandardButton.No),
    )
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)

    assert window.confirm_large_download(job, 4 * 1024**3) is False
    assert "4.0 GiB" in window.statusBar().currentMessage()


def test_the_default_button_is_the_safe_one(window, monkeypatch, tmp_path):
    """A stray Enter must not start a four-gigabyte download."""
    captured: dict[str, object] = {}

    def question(parent, title, text, buttons, default):
        captured["default"] = default
        return default

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)

    assert window.confirm_large_download(job, 4 * 1024**3) is False
    assert captured["default"] == QMessageBox.StandardButton.No


# --- when it is asked ------------------------------------------------------


def media_of(total: int) -> Media:
    media = Media(provider="test", original_url="https://provider.test/x", title="V")
    media.sources = [
        MediaSource(
            url="https://cdn.test/v.mp4",
            source_type="HTTP",
            quality_value=1080,
            expected_size=total,
            track=MediaTrackInfo(role="combined", container="mp4"),
        )
    ]
    return media


def session_for(media: Media) -> ProviderSession:
    core = MagicMock(spec=BaseCore)

    async def download(configuration):
        Path(configuration.path).write_bytes(b"x")
        return True

    core.download = download

    async def close() -> None:
        return None

    core.close = close

    class Registry:
        async def resolve(self, url: str) -> Media:
            return media

        async def close(self) -> None:
            return None

    return ProviderSession(registry=Registry(), core=core)


@pytest.mark.asyncio
async def test_a_small_download_is_never_asked_about(tmp_path):
    asked: list[int] = []
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)
    job.confirm_large_download = lambda _job, size: (asked.append(size), True)[1]

    await run_download_job(
        job, session_factory=lambda: session_for(media_of(LARGE_DOWNLOAD_BYTES - 1))
    )

    assert asked == []
    assert job.state == LifecycleState.COMPLETED, job.error


@pytest.mark.asyncio
async def test_the_question_comes_before_the_first_byte(tmp_path):
    """Refusing after the download would save nothing, so it is asked first."""
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)
    job.confirm_large_download = lambda _job, size: False

    await run_download_job(
        job, session_factory=lambda: session_for(media_of(3 * 1024**3))
    )

    assert job.state == LifecycleState.FAILED
    assert "GiB" in job.error
    assert list(tmp_path.glob("*.mp4")) == [], "nothing may have been written"


@pytest.mark.asyncio
async def test_a_provider_that_states_no_size_cannot_be_asked_about(tmp_path):
    """No estimate is not a small download - it is an unknown one, and the
    alternative would be a dialog on every job from such a provider."""
    asked: list[int] = []
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)
    job.confirm_large_download = lambda _job, size: (asked.append(size), True)[1]

    await run_download_job(job, session_factory=lambda: session_for(media_of(None)))

    assert asked == []
    assert job.state == LifecycleState.COMPLETED, job.error
