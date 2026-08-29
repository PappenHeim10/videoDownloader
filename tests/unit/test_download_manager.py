import asyncio
import tempfile
import unittest
from pathlib import Path

from base_api.models import Media, MediaSource

from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.provider_session import ProviderSession


class FakeCore:
    """Stands in for BaseCore: records what it was asked to download."""

    def __init__(self, result=True, error: Exception | None = None):
        self.configurations = []
        self.result = result
        self.error = error
        self.close_calls = 0

    async def download(self, configuration):
        self.configurations.append(configuration)
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self):
        self.close_calls += 1


class FakeRegistry:
    """Stands in for ProviderRegistry: one media, or one selection failure."""

    def __init__(self, media: Media | None = None, error: Exception | None = None):
        self.media = media
        self.error = error
        self.resolved: list[str] = []
        self.close_calls = 0

    async def resolve(self, url: str) -> Media:
        self.resolved.append(url)
        if self.error is not None:
            raise self.error
        assert self.media is not None
        return self.media

    async def close(self) -> None:
        self.close_calls += 1


def fake_media(url: str = "a", title: str = "test") -> Media:
    return Media(
        provider="fake",
        original_url=url,
        title=title,
        sources=[MediaSource(url="https://cdn.test/stream.m3u8", source_type="HLS")],
    )


def fake_session(*, media: Media | None = None, resolve_error: Exception | None = None,
                 download_error: Exception | None = None, result=True):
    registry = FakeRegistry(media=media or fake_media(), error=resolve_error)
    core = FakeCore(result=result, error=download_error)
    return ProviderSession(registry=registry, core=core), core


class DownloadManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_event_is_loop_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            job = DownloadJob(url="a", quality="best", output_dir=Path(directory))

            self.assertIsInstance(job.stop_event, asyncio.Event)
            self.assertTrue(asyncio.iscoroutinefunction(job.stop_event.wait))

            waiter = asyncio.create_task(job.stop_event.wait())
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())

            job.request_stop()
            await asyncio.wait_for(waiter, timeout=0.5)

    async def test_jobs_have_independent_tasks_events_and_state_files(self):
        with tempfile.TemporaryDirectory() as directory:
            async def runner(job):
                job.update_progress(25, 100)
                while not job.stop_event.is_set():
                    await asyncio.sleep(0.001)
                job.transition(LifecycleState.CANCELLED)
                return job

            manager = DownloadManager(directory, job_runner=runner)
            job_a = manager.add_download("a")
            job_b = manager.add_download("b")
            await asyncio.sleep(0)
            self.assertIsNot(job_a.asyncio_task, job_b.asyncio_task)
            self.assertIsNot(job_a.stop_event, job_b.stop_event)
            self.assertNotEqual(job_a.state_file, job_b.state_file)
            self.assertEqual(job_a.progress, 25)
            await manager.shutdown()

    async def test_run_download_job_rejects_blocking_stop_event(self):
        with tempfile.TemporaryDirectory() as directory:
            class BlockingStopEvent:
                def __init__(self):
                    self._set = False

                def is_set(self):
                    return self._set

                def set(self):
                    self._set = True

                def wait(self):
                    return True

            from video_downloader.application.download_service import run_download_job

            job = DownloadJob(url="a", quality="best", output_dir=Path(directory))
            job.stop_event = BlockingStopEvent()  # type: ignore[assignment]

            session, core = fake_session()
            await run_download_job(job, session_factory=lambda: session)

            self.assertEqual(job.state, LifecycleState.FAILED)
            self.assertIsNotNone(job.error)
            self.assertIn("async wait", job.error)
            self.assertEqual(core.configurations, [])  # nothing was downloaded

    async def test_cancel_isolation_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            async def runner(job):
                while not job.stop_event.is_set():
                    await asyncio.sleep(0.001)
                job.transition(LifecycleState.CANCELLED)
                return job

            manager = DownloadManager(directory, job_runner=runner)
            job_a = manager.add_download("a")
            job_b = manager.add_download("b")
            await manager.cancel_download(job_a)
            self.assertEqual(job_a.state, LifecycleState.CANCELLED)
            self.assertFalse(job_b.stop_event.is_set())
            await manager.delete_download(job_a)
            self.assertNotIn(job_a, manager.get_jobs())
            await manager.shutdown()

    async def test_failure_isolation_and_provider_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = []

            from video_downloader.application.download_service import run_download_job

            def factory():
                session, core = fake_session(download_error=RuntimeError("broken"))
                sessions.append((session, core))
                return session

            async def runner(job):
                return await run_download_job(job, session_factory=factory)

            manager = DownloadManager(directory, job_runner=runner)
            job = manager.add_download("broken")
            await job.asyncio_task
            self.assertEqual(job.state, LifecycleState.FAILED)
            # A failing download still releases the job's provider resources.
            session, core = sessions[0]
            self.assertEqual(core.close_calls, 1)
            self.assertEqual(session.registry.close_calls, 1)
            await manager.shutdown()


if __name__ == "__main__":
    unittest.main()