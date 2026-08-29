from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable

from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.application.download_service import run_download_job

logger = logging.getLogger(__name__)


class DownloadManager:
    def __init__(
        self,
        output_dir: str | Path = "downloads",
        max_concurrent_downloads: int | None = None,
        job_runner: Callable[[DownloadJob], object] = run_download_job,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / ".state").mkdir(exist_ok=True)
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads) if max_concurrent_downloads else None
        self._job_runner = job_runner
        self._jobs: dict[str, DownloadJob] = {}
        self._shutdown = False
        self._load_existing_files()

    def _load_existing_files(self) -> None:
        for path in sorted(self.output_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in {".mp4", ".ts"}:
                job = DownloadJob(url="", quality="best", output_dir=self.output_dir, title=path.name)
                job.output_file = path
                job.progress = 100.0
                job.transition(LifecycleState.COMPLETED)
                self._jobs[job.id] = job

    def add_download(self, url: str, quality: str | int = "best", remux: bool = True) -> DownloadJob:
        if self._shutdown:
            raise RuntimeError("DownloadManager ist bereits beendet")
        job = DownloadJob(url=url, quality=quality, output_dir=self.output_dir, remux=remux)
        self._jobs[job.id] = job
        logger.info("[JOB %s] Download added url=%s quality=%s remux=%s", job.id, job.url, job.quality, job.remux)
        job.transition(LifecycleState.QUEUED)
        self.start_download(job)
        return job

    def start_download(self, job: DownloadJob) -> asyncio.Task:
        if job.asyncio_task is not None and not job.asyncio_task.done():
            return job.asyncio_task  # type: ignore[return-value]

        async def execute() -> DownloadJob:
            if self._semaphore is None:
                return await self._job_runner(job)  # type: ignore[misc]
            async with self._semaphore:
                return await self._job_runner(job)  # type: ignore[misc]
                
        logger.info("[JOB %s] Creating task download-%s", job.id, job.id)
        job.asyncio_task = asyncio.create_task(execute(), name=f"download-{job.id}")
        logger.info("[JOB %s] Task download-%s created", job.id, job.id)
        return job.asyncio_task  # type: ignore[return-value]

    async def cancel_download(self, job: DownloadJob) -> None:
        logger.info("[JOB %s] Cancel requested", job.id)
        if job.state in {LifecycleState.COMPLETED, LifecycleState.FAILED, LifecycleState.CANCELLED}:
            return
        job.request_stop()
        if job.asyncio_task is not None:
            await job.asyncio_task

    async def delete_download(self, job: DownloadJob) -> None:
        if job.asyncio_task is not None and not job.asyncio_task.done():
            await self.cancel_download(job)
        if job.output_file is not None:
            job.output_file.unlink(missing_ok=True)
        job.state_file.unlink(missing_ok=True)
        self._jobs.pop(job.id, None)

    def get_jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    async def shutdown(self) -> None:
        self._shutdown = True
        active = [job for job in self._jobs.values() if job.asyncio_task is not None and not job.asyncio_task.done()]
        for job in active:
            job.request_stop()
        if active:
            await asyncio.gather(*(job.asyncio_task for job in active), return_exceptions=True)