from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from base_api.models import Media, MediaSource
from base_api.modules.config import DownloadConfigHLS
from base_api.modules.errors import (
    AmbiguousProviderError,
    UnsupportedProtocolError,
    UnsupportedURLError,
)
from base_api.modules.static_functions import strip_title

from video_downloader.application.provider_session import (
    ProviderNotConfiguredError,
    ProviderSession,
)
from video_downloader.domain.download_job import DownloadJob, LifecycleState

logger = logging.getLogger(__name__)

#: Failures of provider *selection*, raised by the registry before any provider
#: did any work. Kept as their own tuple so they stay distinguishable from
#: network, extraction, segment, remux and filesystem errors - all of which mean
#: a provider was found and something later went wrong.
PROVIDER_SELECTION_ERRORS = (UnsupportedURLError, AmbiguousProviderError)


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


def _hls_source(media: Media) -> MediaSource:
    """Pick the source the HLS engine can actually download.

    The same rule and the same error type the provider used to apply inside its
    own `download()`, so a media without a usable stream still fails as
    UnsupportedProtocolError rather than as something new.
    """
    source = next(
        (s for s in getattr(media, "sources", ()) if getattr(s, "source_type", None) == "HLS"),
        None,
    )
    if source is None:
        raise UnsupportedProtocolError(
            f"No HLS source available for {getattr(media, 'original_url', '')}"
        )
    return source


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
        # The downloader's success contract carries no path: BaseCore.download is
        # annotated DownloadReport | bool, returns True unless return_report is
        # requested, and DownloadReport has no path-like field at all. Assigning the
        # lookup result unconditionally therefore replaced the path we computed
        # before the download with None on every successful run - which is exactly
        # the file that had just been written.
        # Only adopt a result path when there actually is one, so a downloader that
        # someday reports an authoritative location can still override us.
        resolved = _result_path(result)
        if resolved is not None:
            job.output_file = resolved
        job.transition(LifecycleState.COMPLETED)
        job.state_file.unlink(missing_ok=True)


async def run_download_job(
    job: DownloadJob,
    session_factory: Callable[[], ProviderSession] | None = None,
    remux: bool = True,
) -> DownloadJob:
    """Run one job: registry -> Media -> HLS source -> BaseCore -> file.

    Nothing here knows which website the URL belongs to. `session_factory` comes
    from the composition root and supplies both halves of a job's provider
    resources; this function owns their shutdown, on every exit path.
    """
    logger.info("[JOB %s] Starting download task (Thread: %s)", job.id, threading.current_thread().name)
    session: ProviderSession | None = None
    job.output_dir.mkdir(parents=True, exist_ok=True)
    job.state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if session_factory is None:
            raise ProviderNotConfiguredError(
                "Kein Provider konfiguriert. Der DownloadManager braucht einen "
                "job_runner mit Provider-Session (siehe bootstrap)."
            )

        job.transition(LifecycleState.CONNECTING)
        logger.debug("[JOB %s] Creating provider session (Thread: %s)", job.id, threading.current_thread().name)
        session = session_factory()

        job.transition(LifecycleState.FETCHING_METADATA)
        logger.info("[JOB %s] resolve start URL: %s", job.id, job.url)
        media = await session.registry.resolve(job.url)
        logger.info("[JOB %s] resolve finished, provider=%s", job.id, getattr(media, "provider", "unknown"))

        job.title = getattr(media, "title", None) or job.url
        # job.title stays the display title the UI shows. Only the filesystem
        # component is sanitized - through the same function the provider used to
        # apply inside its own download(). The sanitized path is now handed to the
        # engine verbatim, so the path we record and the file that lands on disk
        # are one string rather than two derivations that have to agree.
        job.output_file = job.output_dir / f"{strip_title(job.title)}.mp4"
        source = _hls_source(media)
        job.transition(LifecycleState.DOWNLOADING)
        _ensure_async_stop_event(job)

        callback = _create_progress_callback(job)

        real_remux = job.remux if job.remux is not None else remux
        logger.info("[JOB %s] core.download start (remux=%s)", job.id, real_remux)
        result = await session.core.download(
            DownloadConfigHLS(
                quality=job.quality,
                path=str(job.output_file),
                callback=callback,
                stop_event=job.stop_event,
                media_source=source,
                remux=real_remux,
                segment_state_path=str(job.state_file),
            )
        )
        logger.info("[JOB %s] core.download finished. Result: %s", job.id, result)

        _handle_download_result(job, result)

    except asyncio.CancelledError:
        logger.info("[JOB %s] CancelledError caught", job.id)
        job.request_stop()
        job.transition(LifecycleState.CANCELLED)
        raise
    except PROVIDER_SELECTION_ERRORS as error:
        # No provider ran, so nothing was fetched and nothing was written. Logged
        # apart from the generic failure below so that "this link is not ours"
        # never reads like a network or extraction problem.
        job.error = f"{type(error).__name__}: {error}"
        job.transition(LifecycleState.FAILED)
        logger.warning("[JOB %s] Provider selection failed for %s: %s", job.id, job.url, error)
    except Exception as error:
        job.error = f"{type(error).__name__}: {error}"
        job.transition(LifecycleState.FAILED)
        logger.exception("[JOB %s] Download failed", job.id)
    finally:
        if session is not None:
            logger.info("[JOB %s] provider session close start", job.id)
            await session.close()
            logger.info("[JOB %s] provider session close finish", job.id)
        logger.info("[JOB %s] Job ended with state %s", job.id, job.state.value)
    return job
