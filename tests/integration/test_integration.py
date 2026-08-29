import asyncio
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from PySide6.QtWidgets import QApplication

import video_downloader.__main__ as main
import debug_main
from video_downloader.application.download_manager import DownloadManager
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
    
    # wait for fast fail due to invalid URL
    await task
    
    # cancel_download is async
    assert asyncio.iscoroutinefunction(manager.cancel_download)
    await manager.cancel_download(job)
    
    # delete_download is async
    assert asyncio.iscoroutinefunction(manager.delete_download)
    await manager.delete_download(job)

# 7. Fake Complete Download Integration

@pytest.mark.asyncio
async def test_fake_complete_download():
    with tempfile.TemporaryDirectory() as td:
        class FakeVideo:
            title = "test_vid"
            async def download(self, **kwargs):
                cb = kwargs["callback"]
                stop = kwargs["stop_event"]
                for i in range(1, 5):
                    if stop.is_set():
                        return type("Result", (), {"status": "cancelled"})()
                    cb(i, 4)
                return type("Result", (), {"status": "completed", "output_file": "test.mp4"})()

        class FakeClient:
            def __init__(self):
                self.core = AsyncMock()
            async def get_video(self, url):
                return FakeVideo()

        async def runner(job):
            return await run_download_job(job, client_factory=FakeClient)
            
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
        class FakeClientFail:
            def __init__(self):
                self.core = AsyncMock()
            async def get_video(self, url):
                raise ConnectionError("No network")
                
        async def runner(job):
            return await run_download_job(job, client_factory=FakeClientFail)
            
        manager = DownloadManager(output_dir=td, job_runner=runner)
        job = manager.add_download("http://fail")
        await job.asyncio_task
        assert job.state == LifecycleState.FAILED
        assert "ConnectionError" in job.error

@pytest.mark.asyncio
async def test_fake_cancel_download():
    with tempfile.TemporaryDirectory() as td:
        class FakeVideoBlock:
            async def download(self, **kwargs):
                stop = kwargs["stop_event"]
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                return type("Result", (), {"status": "cancelled"})()
                
        class FakeClientBlock:
            def __init__(self):
                self.core = AsyncMock()
            async def get_video(self, url):
                return FakeVideoBlock()
                
        async def runner(job):
            return await run_download_job(job, client_factory=FakeClientBlock)
            
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
