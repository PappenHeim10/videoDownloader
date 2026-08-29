from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from xhamster_api import Client

from video_downloader.domain.download_job import DownloadJob, LifecycleState

logger = logging.getLogger(__name__)


def _result_path(result: Any) -> Path | None:
    for name in ("output_file", "output_path", "path", "filename", "file"):
        value = getattr(result, name, None)
        if value:
            return Path(value)
    if isinstance(result, (str, Path)):
        return Path(result)
    return None


def _ensure_async_stop_event(job: DownloadJob) -> None:
    wait_method = getattr(job.stop_event, "wait", None)
    if wait_method is None or not asyncio.iscoroutinefunction(wait_method):
        raise TypeError(
            "DownloadJob.stop_event must support async wait(); use asyncio.Event for loop-safe cancellation."
        )


def _create_progress_callback(job: DownloadJob) -> Callable[[int, int], None]:
    state = {
        "last_log_time": 0.0,
        "last_progress_emit_time": 0.0,
        "last_progress_emit_value": -1,
    }

    def callback(downloaded: int, total: int) -> None:
        now = time.monotonic()
        if now - state["last_log_time"] >= 0.5 or downloaded == total:
            logger.debug(
                "[JOB %s] progress callback downloaded=%s total=%s (Thread: %s)",
                job.id, downloaded, total, threading.current_thread().name
            )
            state["last_log_time"] = now

        # Coalesce high-frequency progress callbacks to keep UI and event loop responsive.
        should_emit = downloaded == total
        if not should_emit:
            should_emit = (
                downloaded != state["last_progress_emit_value"]
                and (now - state["last_progress_emit_time"]) >= 0.15
            )
        if should_emit:
            job.update_progress(downloaded, total)
            state["last_progress_emit_time"] = now
            state["last_progress_emit_value"] = downloaded

    return callback


def _handle_download_result(job: DownloadJob, result: Any) -> None:
    result_status = getattr(result, "status", None)
    if job.stop_event.is_set():
        logger.info("[JOB %s] Stop event was set", job.id)

    if job.stop_event.is_set() or result_status == "cancelled":
        job.transition(LifecycleState.CANCELLED)
    elif result is False or (result_status is not None and result_status != "completed"):
        job.error = "Download blieb unvollständig."
        job.transition(LifecycleState.FAILED)
    else:
        job.progress = 100.0 if job.total_segments else job.progress
        job.output_file = _result_path(result)
        job.transition(LifecycleState.COMPLETED)
        job.state_file.unlink(missing_ok=True)


async def run_download_job(
    job: DownloadJob,
    client_factory: Callable[[], Client] = Client,
    remux: bool = True,
) -> DownloadJob:
    logger.info("[JOB %s] Starting download task (Thread: %s)", job.id, threading.current_thread().name)
    client: Client | None = None
    job.output_dir.mkdir(parents=True, exist_ok=True)
    job.state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        job.transition(LifecycleState.CONNECTING)
        logger.debug("[JOB %s] Creating client (Thread: %s)", job.id, threading.current_thread().name)
        client = client_factory()

        job.transition(LifecycleState.FETCHING_METADATA)
        logger.info("[JOB %s] get_video start URL: %s", job.id, job.url)
        video = await client.get_video(job.url)
        logger.info("[JOB %s] get_video finished", job.id)

        job.title = getattr(video, "title", None) or job.url
        job.output_file = job.output_dir / f"{job.title}.mp4"
        job.transition(LifecycleState.DOWNLOADING)
        _ensure_async_stop_event(job)

        callback = _create_progress_callback(job)

        real_remux = job.remux if job.remux is not None else remux
        logger.info("[JOB %s] video.download start (remux=%s)", job.id, real_remux)
        result = await video.download(
            quality=job.quality,
            path=str(job.output_dir),
            callback=callback,
            remux=real_remux,
            stop_event=job.stop_event,
            segment_state_path=str(job.state_file),
        )
        logger.info("[JOB %s] video.download finished. Result: %s", job.id, result)

        _handle_download_result(job, result)

    except asyncio.CancelledError:
        logger.info("[JOB %s] CancelledError caught", job.id)
        job.request_stop()
        job.transition(LifecycleState.CANCELLED)
        raise
    except Exception as error:
        job.error = f"{type(error).__name__}: {error}"
        job.transition(LifecycleState.FAILED)
        logger.exception("[JOB %s] Download failed", job.id)
    finally:
        if client is not None:
            logger.info("[JOB %s] client.close start", job.id)
            await client.core.close()
            logger.info("[JOB %s] client.close finish", job.id)
        logger.info("[JOB %s] Job ended with state %s", job.id, job.state.value)
    return job
