import asyncio
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QApplication

from base_api.models import Media, MediaSource

import video_downloader.__main__ as main
import debug_main
from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.application.download_service import run_download_job
from video_downloader.ui.main_window import MainWindow
from video_downloader.infrastructure.settings import AppSettings

# 1/2. Imports and Entry point tests

def test_production_entrypoint_calls_application():
    with patch("video_downloader.__main__.run_application", return_value=0) as runner:
        import video_downloader.__main__ as main
        with pytest.raises(SystemExit) as exc_info:
            main.main()
        assert exc_info.value.code == 0
        runner.assert_called_once_with(debug=False, smoke_test=False)

@pytest.mark.timeout(10)
def test_debug_entrypoint_calls_application_in_debug_mode():
    with patch("debug_main.run_application", return_value=0) as runner:
        import debug_main
        with pytest.raises(SystemExit) as exc_info:
            debug_main.main()
        assert exc_info.value.code == 0
        runner.assert_called_once_with(debug=True, smoke_test=False)

# 3. smoke-test parameter through run_application

def test_smoke_test_parameter():
    with patch("sys.argv", ["main.py", "--smoke-test"]):
        with patch("video_downloader.__main__.run_application", return_value=0) as runner:
            import video_downloader.__main__ as main
            with pytest.raises(SystemExit) as exc_info:
                main.main()
            assert exc_info.value.code == 0
            runner.assert_called_once_with(debug=False, smoke_test=True)

# 4. Constructor and Object Wiring

@pytest.mark.asyncio
async def test_component_construction():
    manager = DownloadManager(output_dir="downloads_test", max_concurrent_downloads=2)
    assert manager._semaphore is not None
    job = manager.add_download(url="dummy_url", quality="1080", remux=False)
    assert isinstance(job, DownloadJob)
    assert job.url == "dummy_url"
    assert job.quality == "1080"
    assert job.remux is False
    assert job.state == LifecycleState.QUEUED
    await manager.shutdown()

# 5. Public Methods & GUI Wiring

@pytest.fixture
def qt_app():
    app = QApplication.instance()
    if not app:
        app = QApplication([])
    return app

def test_gui_to_manager_wiring(qt_app, tmp_path, monkeypatch):
    # Isolated settings root: a test must never read or write the real AppData.
    monkeypatch.setenv("VIDEO_DOWNLOADER_HOME", str(tmp_path / "appdata"))
    target = tmp_path / "videos"
    target.mkdir()
    settings = AppSettings()
    settings.set_download_directory(target)

    manager = MagicMock(spec=DownloadManager)
    manager.get_jobs.return_value = []

    # Needs to return a mock job
    dummy_job = DownloadJob(url="a", quality="best", output_dir=target)
    manager.add_download.return_value = dummy_job

    window = MainWindow(manager, settings)
    window.url.setText("http://test")
    window.quality.setText("720")
    window.add_download()

    # The window now resolves the destination and hands it down explicitly; the
    # manager no longer decides where anything lands.
    manager.add_download.assert_called_once_with(
        "http://test", "720", output_dir=target.resolve()
    )

# 6. Async Call Boundaries

@pytest.mark.asyncio
async def test_async_method_boundaries(tmp_path):
    # An explicit directory: the manager has no default any more, by design.
    manager = DownloadManager(tmp_path)
    
    # start_download is synchronous but returns a Task
    job = manager.add_download("a")
    task = manager.start_download(job)
    assert asyncio.isfuture(task) or isinstance(task, asyncio.Task)
    
    # Fails fast and without network: the bare manager has no provider wiring.
    await task
    
    # cancel_download is async
    assert asyncio.iscoroutinefunction(manager.cancel_download)
    await manager.cancel_download(job)
    
    # delete_download is async
    assert asyncio.iscoroutinefunction(manager.delete_download)
    await manager.delete_download(job)

# 7. Fake Complete Download Integration

def _media(url: str, title: str = "test_vid") -> Media:
    return Media(
        provider="fake",
        original_url=url,
        title=title,
        sources=[MediaSource(url="https://cdn.test/stream.m3u8", source_type="HLS")],
    )


class _FakeRegistry:
    def __init__(self, media: Media | None = None, error: Exception | None = None):
        self.media = media
        self.error = error

    async def resolve(self, url):
        if self.error is not None:
            raise self.error
        return self.media or _media(url)

    async def close(self):
        return None


class _FakeCore:
    """A BaseCore stand-in driven by the DownloadConfigHLS it receives."""

    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def download(self, configuration):
        return await self.behaviour(configuration)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_fake_complete_download():
    with tempfile.TemporaryDirectory() as td:
        async def behaviour(configuration):
            for i in range(1, 5):
                if configuration.stop_event.is_set():
                    return type("Result", (), {"status": "cancelled"})()
                configuration.callback(i, 4)
            return type("Result", (), {"status": "completed", "output_file": "test.mp4"})()

        def factory():
            return ProviderSession(registry=_FakeRegistry(), core=_FakeCore(behaviour))

        async def runner(job):
            return await run_download_job(job, session_factory=factory)

        manager = DownloadManager(output_dir=td, job_runner=runner)
        job = manager.add_download("http://ok")
        await job.asyncio_task

        assert job.state == LifecycleState.COMPLETED
        assert job.progress == 100.0
        assert not job.state_file.exists()

# 8. Failure Path Testing

@pytest.mark.asyncio
async def test_fake_failed_metadata():
    with tempfile.TemporaryDirectory() as td:
        async def never(configuration):  # pragma: no cover - must not run
            raise AssertionError("resolution failed; nothing may be downloaded")

        def factory():
            return ProviderSession(
                registry=_FakeRegistry(error=ConnectionError("No network")),
                core=_FakeCore(never),
            )

        async def runner(job):
            return await run_download_job(job, session_factory=factory)

        manager = DownloadManager(output_dir=td, job_runner=runner)
        job = manager.add_download("http://fail")
        await job.asyncio_task
        assert job.state == LifecycleState.FAILED
        assert "ConnectionError" in job.error

@pytest.mark.asyncio
async def test_fake_cancel_download():
    with tempfile.TemporaryDirectory() as td:
        async def behaviour(configuration):
            while not configuration.stop_event.is_set():
                await asyncio.sleep(0.01)
            return type("Result", (), {"status": "cancelled"})()

        def factory():
            return ProviderSession(registry=_FakeRegistry(), core=_FakeCore(behaviour))

        async def runner(job):
            return await run_download_job(job, session_factory=factory)

        manager = DownloadManager(output_dir=td, job_runner=runner)
        job = manager.add_download("http://cancel")
        await asyncio.sleep(0.05)
        await manager.cancel_download(job)
        assert job.state == LifecycleState.CANCELLED

# 9. Concurrency / Two Downloads

@pytest.mark.asyncio
async def test_multiple_concurrent_fakes():
    with tempfile.TemporaryDirectory() as td:
        async def slow_runner(job):
            await asyncio.sleep(0.05)
            if job.url == "fail":
                job.transition(LifecycleState.FAILED)
            else:
                job.transition(LifecycleState.COMPLETED)
            return job
            
        manager = DownloadManager(output_dir=td, job_runner=slow_runner)
        job1 = manager.add_download("fail")
        job2 = manager.add_download("ok")
        
        assert job1.state_file != job2.state_file
        assert job1.asyncio_task != job2.asyncio_task
        assert job1.stop_event != job2.stop_event
        
        await asyncio.gather(job1.asyncio_task, job2.asyncio_task)
        assert job1.state == LifecycleState.FAILED
        assert job2.state == LifecycleState.COMPLETED
