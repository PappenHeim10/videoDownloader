import asyncio
import tempfile
import unittest
from pathlib import Path

from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.application.download_manager import DownloadManager


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

            class Core:
                async def close(self):
                    return None

            class Video:
                title = "test"

                async def download(self, **kwargs):
                    return True

            class Client:
                def __init__(self):
                    self.core = Core()

                async def get_video(self, url):
                    return Video()

            from video_downloader.application.download_service import run_download_job

            job = DownloadJob(url="a", quality="best", output_dir=Path(directory))
            job.stop_event = BlockingStopEvent()  # type: ignore[assignment]

            await run_download_job(job, client_factory=Client)

            self.assertEqual(job.state, LifecycleState.FAILED)
            self.assertIsNotNone(job.error)
            self.assertIn("async wait", job.error)

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

    async def test_failure_isolation_and_client_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            clients = []

            class Core:
                def __init__(self):
                    self.closed = False

                async def close(self):
                    self.closed = True

            class Video:
                async def download(self, **kwargs):
                    raise RuntimeError("broken")

            class Client:
                def __init__(self):
                    self.core = Core()
                    clients.append(self)

                async def get_video(self, url):
                    return Video()

            from video_downloader.application.download_service import run_download_job

            async def runner(job):
                return await run_download_job(job, client_factory=Client)

            manager = DownloadManager(directory, job_runner=runner)
            job = manager.add_download("broken")
            await job.asyncio_task
            self.assertEqual(job.state, LifecycleState.FAILED)
            self.assertTrue(clients[0].core.closed)
            await manager.shutdown()


if __name__ == "__main__":
    unittest.main()