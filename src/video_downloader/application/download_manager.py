from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.application.download_service import run_download_job

logger = logging.getLogger(__name__)

#: What a finished download can be called on disk. `.webm` and `.mkv` joined the
#: list when the output container stopped always being MP4: a video muxed from
#: VP9 and Opus is a WebM, and a directory scan that did not know that would
#: show yesterday's downloads as missing.
FINISHED_SUFFIXES = frozenset({".mp4", ".ts", ".webm", ".mkv"})

#: How long a stopped job is given to end on its own before the task is
#: cancelled outright. Measured against a real failure: a 657 MB YouTube
#: track stopped transferring the moment the stop event was set and then
#: never returned from `core.download()`, so the cancel never came back and
#: the entry never left the list. A cancel that waits without limit on the
#: thing it is cancelling has no way to fail visibly - it just hangs.
CANCEL_GRACE_SECONDS = 10.0


class DownloadManager:
    def __init__(
        self,
        output_dir: str | Path | None = None,
        max_concurrent_downloads: int | None = None,
        job_runner: Callable[[DownloadJob], object] = run_download_job,
        cancel_grace_seconds: float = CANCEL_GRACE_SECONDS,
    ) -> None:
        # No default directory on purpose. Choosing one is a user decision made
        # above this layer; falling back to a relative path here is exactly how
        # the destination used to depend on the working directory.
        self.output_dir = self._prepare(output_dir) if output_dir is not None else None
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads) if max_concurrent_downloads else None
        self._job_runner = job_runner
        self._jobs: dict[str, DownloadJob] = {}
        self._cancel_grace = cancel_grace_seconds
        # One stop per job, however many callers ask for it. Without this a
        # user clicking twice starts a second wait on the same task.
        self._stopping: dict[str, asyncio.Future] = {}
        self._shutdown = False
        if self.output_dir is not None:
            self.rescan_output_directory()

    @staticmethod
    def _prepare(output_dir: str | Path) -> Path:
        resolved = Path(output_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        (resolved / ".state").mkdir(exist_ok=True)
        return resolved

    def rescan_output_directory(self, output_dir: str | Path | None = None) -> None:
        """Lies die fertigen Dateien im Zielordner neu ein.

        Frueher lief das genau einmal, im Konstruktor. Wer danach den Zielordner
        wechselte, sah weiter die Dateien des alten - die Liste beschrieb ein
        Verzeichnis, in das nichts mehr geschrieben wurde.

        Eintraege aus einem frueheren Scan werden verworfen, bevor neu gelesen
        wird: sie beschreiben ein anderes Verzeichnis. Jobs dieser Sitzung
        bleiben unangetastet, auch wenn ihre Datei woanders liegt - sie gehoeren
        dem Benutzer und nicht dem Scan.

        Der Aufrufer liest danach `get_jobs()` erneut; diese Methode meldet
        nichts zurueck, weil sowohl Zugaenge als auch Abgaenge entstehen und
        eine Liste beides schon ausdrueckt.
        """
        if output_dir is not None:
            self.output_dir = self._prepare(output_dir)
        if self.output_dir is None:
            return

        for job_id in [job.id for job in self._jobs.values() if job.from_disk]:
            self._jobs.pop(job_id, None)

        # Eine Datei, die bereits zu einem Job dieser Sitzung gehoert, taucht
        # nicht ein zweites Mal als Fund auf.
        claimed = {
            job.output_file for job in self._jobs.values() if job.output_file is not None
        }
        for path in sorted(self.output_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in FINISHED_SUFFIXES:
                continue
            if path in claimed:
                continue
            job = DownloadJob(
                url="",
                quality="best",
                output_dir=self.output_dir,
                title=path.name,
                from_disk=True,
            )
            job.output_file = path
            job.mark_completed()
            self._jobs[job.id] = job

    def add_download(
        self,
        url: str,
        quality: str | int = "best",
        remux: bool = True,
        output_dir: str | Path | None = None,
        confirm_large_download: Callable[[DownloadJob, int], Awaitable[bool]] | None = None,
        request_login: Callable[[Exception], Awaitable[bool]] | None = None,
    ) -> DownloadJob:
        if self._shutdown:
            raise RuntimeError("DownloadManager ist bereits beendet")

        # The caller may pin a directory per job. That is what makes a settings
        # change apply to future jobs only: running and finished jobs keep the
        # directory they were created with.
        target = self._prepare(output_dir) if output_dir is not None else self.output_dir
        if target is None:
            raise ValueError(
                "Kein Download-Verzeichnis konfiguriert. "
                "Waehle eines in der GUI oder uebergib output_dir."
            )

        # Passed in per job rather than held by the manager: only a caller with
        # a window can ask a question, and the manager runs headless just as
        # often as it runs behind one.
        job = DownloadJob(
            url=url,
            quality=quality,
            output_dir=target,
            remux=remux,
            confirm_large_download=confirm_large_download,
            request_login=request_login,
        )
        # Von hier an erreichen Fortschrittsmeldungen diesen Job auch aus
        # Worker-Threads; ab jetzt weiss er, worauf er sie anwendet.
        job.bind_loop()
        self._jobs[job.id] = job
        logger.info("[JOB %s] Download added url=%s quality=%s remux=%s", job.id, job.url, job.quality, job.remux)
        job.transition(LifecycleState.QUEUED)
        self.start_download(job)
        return job

    def start_download(self, job: DownloadJob) -> asyncio.Task:
        job.bind_loop()
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
        """Stop a running job and leave on disk whatever is already there.

        Deliberately without deleting: the resume state and a half-fetched track
        are exactly what makes a later attempt cheap, and a finished output file
        belongs to the user. Deleting is said out loud, through
        `delete_download(..., delete_file=True)`.

        Returns when the job is no longer running - which is not the same as
        "when the job agreed to stop". See `_stop`.
        """
        logger.info("[JOB %s] Cancel requested", job.id)
        pending = self._stopping.get(job.id)
        if pending is None:
            pending = asyncio.ensure_future(self._stop(job))
            self._stopping[job.id] = pending
            pending.add_done_callback(
                lambda _finished, job_id=job.id: self._stopping.pop(job_id, None)
            )
        # Shielded: a caller that is itself cancelled must not take the stop
        # down with it, or the next caller would start over on a job that is
        # already halfway stopped.
        await asyncio.shield(pending)

    async def _stop(self, job: DownloadJob) -> None:
        """Ask the job to stop, and make sure it actually does.

        Two steps, deliberately separate. First the stop event and a grace
        period - the ordinary path, where the download notices and unwinds
        itself, closing its provider session on the way out. Then, only if that
        did not happen, the task is cancelled outright.

        The grace period exists because a download that stops cleanly is worth
        waiting a moment for; the cancellation exists because the engine has
        been observed to stop transferring and then never return. Waiting for
        that without a limit is what left a dead entry in the list and no way
        to remove it.
        """
        if job.is_finished:
            return
        job.request_stop()

        task = job.asyncio_task
        if task is None or getattr(task, "done", lambda: True)():
            self._mark_cancelled(job)
            return

        if await self._settled(task):
            return

        logger.warning(
            "[JOB %s] Did not stop within %.1fs; cancelling the task.",
            job.id, self._cancel_grace,
        )
        task.cancel()
        if not await self._settled(task):
            # Nothing left to try. The job is abandoned rather than awaited
            # forever: the alternative is an interface that cannot be used.
            logger.error(
                "[JOB %s] Task still running after cancellation; abandoning it.", job.id
            )
        self._mark_cancelled(job)

    async def _settled(self, task: Any) -> bool:
        """Wait out the grace period. True when the task ended within it.

        Shielded, so the timeout is a plain wait and not a cancellation -
        cancelling is the caller's next step and stays explicit.
        """
        try:
            await asyncio.wait_for(asyncio.shield(task), self._cancel_grace)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            # The job's own `except CancelledError` already reported the state.
            return True
        except Exception:  # noqa: BLE001 - the job logged its own failure
            return True
        return True

    def _mark_cancelled(self, job: DownloadJob) -> None:
        if not job.is_finished:
            job.transition(LifecycleState.CANCELLED)

    async def delete_download(self, job: DownloadJob, *, delete_file: bool) -> None:
        """Remove the job from the registry - and the file only on request.

        `delete_file` deliberately has no default. For an entry found by a
        directory scan, deleting removes a real video file that nobody
        downloaded in this session; no caller may inherit that by accident, so
        it has to be stated at every call site.

        Cancelling is a different thing and lives in `cancel_download`: it stops
        the run and leaves everything on disk where it is.
        """
        if job.asyncio_task is not None and not job.asyncio_task.done():
            await self.cancel_download(job)
        if delete_file and job.output_file is not None:
            job.output_file.unlink(missing_ok=True)
        # Immer: der Job ist gleich weg, und sein Resume-State ist auf seine ID
        # ausgestellt. Ihn liegen zu lassen, hiesse ihn fuer immer liegen zu lassen.
        job.state_file.unlink(missing_ok=True)
        self._jobs.pop(job.id, None)

    def get_jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    async def shutdown(self) -> None:
        """Stop everything that is still running, and come back either way.

        Goes through the same bounded stop as a cancel, and for the same reason:
        this is what the window awaits while it is closing. Gathering the tasks
        directly - which is what it used to do - meant one download that refuses
        to end could keep the application open indefinitely.
        """
        self._shutdown = True
        active = [
            job
            for job in self._jobs.values()
            if job.asyncio_task is not None and not job.asyncio_task.done()
        ]
        if not active:
            return
        await asyncio.gather(
            *(self.cancel_download(job) for job in active), return_exceptions=True
        )