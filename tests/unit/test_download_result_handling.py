"""B1: a successful download must never destroy the path it just wrote to.

The application computes `job.output_file` before the download starts, and that
is the only place the final location is known: the downloader's success contract
carries no path. `BaseCore.download` is annotated `DownloadReport | bool`,
returns `True` unless `return_report` is requested, and `DownloadReport` has no
path-like field. Assigning the lookup result unconditionally therefore cleared
the path on every successful run.

The user-visible fallout was that a finished download could not be opened and
its file survived deletion of the list entry.
"""

from pathlib import Path

import pytest

from base_api.modules.type_hints import DownloadReport

from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.download_service import _handle_download_result, _result_path
from video_downloader.domain.download_job import DownloadJob, LifecycleState


def _completed_job(tmp_path: Path) -> DownloadJob:
    job = DownloadJob(url="https://example.test/v/1", quality="best", output_dir=tmp_path)
    job.title = "A perfectly ordinary title"
    job.output_file = tmp_path / "A perfectly ordinary title.mp4"
    job.total_segments = 10
    job.downloaded_segments = 10
    return job


def _report(status: str = "completed") -> DownloadReport:
    return DownloadReport(
        status=status,
        total=10,
        downloaded=10,
        missing=[],
        missing_urls=[],
        segment_dir=None,
        segment_state_path=None,
        start_segment=0,
        quality="best",
    )


# --- the bug itself ---------------------------------------------------------


def test_success_returning_true_keeps_the_known_output_path(tmp_path):
    """The primary B1 regression: True is what a real successful download returns."""
    job = _completed_job(tmp_path)
    expected = job.output_file

    _handle_download_result(job, True)

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file is not None
    assert job.output_file == expected


def test_success_returning_a_report_without_a_path_keeps_the_known_path(tmp_path):
    job = _completed_job(tmp_path)
    expected = job.output_file

    _handle_download_result(job, _report())

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file == expected


def test_the_downloader_report_genuinely_carries_no_path():
    # Pins the observation the fix rests on. If a future downloader adds a path
    # field, this fails and the adoption branch below becomes the live one.
    assert _result_path(_report()) is None
    assert _result_path(True) is None


def test_an_authoritative_result_path_is_still_adopted(tmp_path):
    # Why _result_path was kept rather than deleted: a result that does carry a
    # location must still win over the pre-computed guess.
    job = _completed_job(tmp_path)
    authoritative = tmp_path / "renamed-by-the-downloader.mp4"

    _handle_download_result(job, str(authoritative))

    assert job.output_file == authoritative


# --- consequences the bug caused -------------------------------------------


def test_a_completed_job_can_be_opened(tmp_path):
    """UI open-eligibility, without launching a player: state + existing path."""
    job = _completed_job(tmp_path)
    job.output_file.write_bytes(b"video")

    _handle_download_result(job, True)

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file is not None
    assert job.output_file.exists()


@pytest.mark.asyncio
async def test_deleting_a_completed_download_also_removes_the_file(tmp_path):
    manager = DownloadManager(output_dir=tmp_path)
    job = _completed_job(manager.output_dir)
    job.output_file.write_bytes(b"video")
    manager._jobs[job.id] = job

    _handle_download_result(job, True)
    await manager.delete_download(job)

    assert not job.output_file.exists()
    assert job.id not in {j.id for j in manager.get_jobs()}


# --- neighbouring behaviour must not shift ---------------------------------


def test_failure_does_not_invent_an_output_path(tmp_path):
    job = _completed_job(tmp_path)
    job.output_file = None

    _handle_download_result(job, False)

    assert job.state == LifecycleState.FAILED
    assert job.output_file is None
    assert job.error


def test_failure_keeps_its_message_and_state(tmp_path):
    job = _completed_job(tmp_path)
    _handle_download_result(job, _report(status="failed"))
    assert job.state == LifecycleState.FAILED


def test_cancellation_is_unchanged(tmp_path):
    job = _completed_job(tmp_path)
    job.request_stop()

    _handle_download_result(job, True)

    assert job.state == LifecycleState.CANCELLED


def test_cancelled_status_from_the_report_is_unchanged(tmp_path):
    job = _completed_job(tmp_path)
    _handle_download_result(job, _report(status="cancelled"))
    assert job.state == LifecycleState.CANCELLED


def test_progress_is_still_completed_to_full(tmp_path):
    job = _completed_job(tmp_path)
    job.progress = 42.0

    _handle_download_result(job, True)

    assert job.progress == 100.0


def test_observers_see_the_final_path(tmp_path):
    job = _completed_job(tmp_path)
    seen: list[tuple[LifecycleState, Path | None]] = []
    job.on_change = lambda j: seen.append((j.state, j.output_file))

    _handle_download_result(job, True)

    assert seen[-1] == (LifecycleState.COMPLETED, job.output_file)
    assert seen[-1][1] is not None
