from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.application.download_service import run_download_job

logger = logging.getLogger(__name__)

#: What a finished download can be called on disk. `.webm` and `.mkv` joined the
#: list when the output container stopped always being MP4: a video muxed from
#: VP9 and Opus is a WebM, and a directory scan that did not know that would
#: show yesterday's downloads as missing.
FINISHED_SUFFIXES = frozenset({".mp4", ".ts", ".webm", ".mkv"})


class DownloadManager:
    def __init__(
        self,
        output_dir: str | Path | None = None,
        max_concurrent_downloads: int | None = None,
        job_runner: Callable[[DownloadJob], object] = run_download_job,
    ) -> None:
        # No default directory on purpose. Choosing one is a user decision made
        # above this layer; falling back to a relative path here is exactly how
        # the destination used to depend on the working directory.
        self.output_dir = self._prepare(output_dir) if output_dir is not None else None
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads) if max_concurrent_downloads else None
        self._job_runner = job_runner
        self._jobs: dict[str, DownloadJob] = {}
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
        """Stoppe den laufenden Job und lass alles liegen, was schon da ist.

        Ausdruecklich ohne Loeschen: der Resume-State und eine halb geladene
        Spur sind genau das, was einen spaeteren Fortsetzungsversuch billig
        macht, und eine fertige Ausgabedatei gehoert dem Benutzer. Wer loeschen
        will, sagt das ueber `delete_download(..., delete_file=True)`.
        """
        logger.info("[JOB %s] Cancel requested", job.id)
        if job.state in {LifecycleState.COMPLETED, LifecycleState.FAILED, LifecycleState.CANCELLED}:
            return
        job.request_stop()
        if job.asyncio_task is not None:
            await job.asyncio_task

    async def delete_download(self, job: DownloadJob, *, delete_file: bool) -> None:
        """Entferne den Job aus der Verwaltung - und die Datei nur auf Ansage.

        `delete_file` hat absichtlich keinen Vorgabewert. Fuer einen Eintrag aus
        dem Verzeichnisscan bedeutet das Loeschen eine echte Videodatei, die
        niemand in dieser Sitzung heruntergeladen hat; das darf kein Aufrufer
        versehentlich erben, sondern muss an jeder Aufrufstelle dastehen.

        Abbrechen ist etwas anderes und steht in `cancel_download`: es stoppt
        den Lauf und laesst alles liegen, was auf der Platte ist.
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
        self._shutdown = True
        active = [job for job in self._jobs.values() if job.asyncio_task is not None and not job.asyncio_task.done()]
        for job in active:
            job.request_stop()
        if active:
            await asyncio.gather(*(job.asyncio_task for job in active), return_exceptions=True)