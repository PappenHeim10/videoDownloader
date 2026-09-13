"""A cancel has to come back, even when the download will not stop.

Found in use rather than in a test, on 2026-09-13. A 657 MB YouTube track was
cancelled: the transfer stopped at once - the last progress callback is the
proof - but `core.download()` never returned. `cancel_download` was waiting on
that task with a plain `await`, so it never returned either. The job stayed in
`downloading`, the entry stayed in the list, and every further click started
another wait that also never came back. The log shows five `Cancel requested`
lines and nothing after them.

The engine's part of that is a separate defect in a separate repository. This
file is about ours, and ours is a design fault rather than a bug: an operation
that waits without limit on the thing it is cancelling has no way to fail
visibly. It can only hang.

So the runner below is deliberately deaf. It ignores the stop event completely,
which is the worst an engine can do, and the tests pin that the interface stays
usable anyway.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from video_downloader.application.download_manager import DownloadManager
from video_downloader.domain.download_job import DownloadJob, LifecycleState

#: Short enough that the tests are tests and not waits. Production keeps the
#: ten seconds from `CANCEL_GRACE_SECONDS`, because a download that unwinds
#: cleanly is worth a moment.
GRACE = 0.05

URL = "https://provider.test/video"


async def _deaf_to_the_stop_event(job: DownloadJob) -> DownloadJob:
    """An engine that stops transferring and then never returns."""
    job.transition(LifecycleState.DOWNLOADING)
    await asyncio.sleep(3600)
    return job


async def _stops_when_told(job: DownloadJob) -> DownloadJob:
    """An engine that behaves - the path the grace period exists for."""
    job.transition(LifecycleState.DOWNLOADING)
    await job.stop_event.wait()
    job.transition(LifecycleState.CANCELLED)
    return job


def a_manager(directory: Path, runner) -> DownloadManager:
    return DownloadManager(directory, job_runner=runner, cancel_grace_seconds=GRACE)


async def a_started_job(manager: DownloadManager) -> DownloadJob:
    job = manager.add_download(URL)
    # Let the task reach its first await, so the cancel has something to stop.
    await asyncio.sleep(0)
    return job


# --- the defect --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_returns_even_when_the_engine_ignores_the_stop_event(tmp_path):
    """The failure exactly as it was seen: the download will not stop."""
    manager = a_manager(tmp_path, _deaf_to_the_stop_event)
    job = await a_started_job(manager)

    # Generous next to the grace period, and still nothing like forever.
    await asyncio.wait_for(manager.cancel_download(job), timeout=5)

    assert job.state is LifecycleState.CANCELLED
    assert job.asyncio_task.done(), "the task was left running"


@pytest.mark.asyncio
async def test_a_second_click_does_not_start_a_second_wait(tmp_path):
    """Five clicks left five hanging coroutines behind; one stop is enough."""
    manager = a_manager(tmp_path, _deaf_to_the_stop_event)
    job = await a_started_job(manager)

    await asyncio.wait_for(
        asyncio.gather(*(manager.cancel_download(job) for _ in range(5))), timeout=5
    )

    assert job.state is LifecycleState.CANCELLED


@pytest.mark.asyncio
async def test_cancelling_an_already_cancelled_job_is_free(tmp_path):
    manager = a_manager(tmp_path, _deaf_to_the_stop_event)
    job = await a_started_job(manager)
    await asyncio.wait_for(manager.cancel_download(job), timeout=5)

    await asyncio.wait_for(manager.cancel_download(job), timeout=1)

    assert job.state is LifecycleState.CANCELLED


# --- and the ordinary path is unchanged --------------------------------------


@pytest.mark.asyncio
async def test_a_download_that_stops_cleanly_is_not_cancelled_outright(tmp_path):
    """The grace period is what lets a job unwind itself and close its session."""
    manager = a_manager(tmp_path, _stops_when_told)
    job = await a_started_job(manager)

    await asyncio.wait_for(manager.cancel_download(job), timeout=5)

    assert job.state is LifecycleState.CANCELLED
    # It ended on its own terms rather than being torn down.
    assert not job.asyncio_task.cancelled()


@pytest.mark.asyncio
async def test_deleting_a_wedged_job_still_removes_it(tmp_path):
    """Delete goes through cancel, so it inherited the same hang."""
    manager = a_manager(tmp_path, _deaf_to_the_stop_event)
    job = await a_started_job(manager)
    output = tmp_path / "half.mp4"
    output.write_bytes(b"partial")
    job.output_file = output

    await asyncio.wait_for(manager.delete_download(job, delete_file=True), timeout=5)

    assert job not in manager.get_jobs()
    assert not output.exists()


@pytest.mark.asyncio
async def test_shutdown_is_not_held_up_by_a_wedged_job(tmp_path):
    """Closing the window must not wait on a download that will not stop."""
    manager = a_manager(tmp_path, _deaf_to_the_stop_event)
    await a_started_job(manager)

    await asyncio.wait_for(manager.shutdown(), timeout=5)
