"""Was ein Schnappschuss traegt - und vor allem, was er nicht traegt.

`DownloadJob` haelt drei Dinge, die kein Konsument je sehen darf: den
`asyncio.Task`, der ihn ausfuehrt, das `asyncio.Event`, mit dem er gestoppt
wird, und die Rueckrufe, mit denen er Fragen stellt. Bis hierher gab es nichts,
was diese Trennung ausgedrueckt haette - das Fenster las die Felder direkt.

Die beiden Richtungen werden einzeln geprueft, weil beide Fehler leicht
passieren: ein Laufzeitobjekt, das hineinrutscht, und ein Feld, das
stillschweigend herausfaellt, wenn jemand dem Job eines hinzufuegt.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from pathlib import Path

from video_downloader.domain.download_job import (
    DownloadJob,
    LifecycleState,
    ProgressUnit,
)
from video_downloader.domain.job_error import ErrorKind, JobError
from video_downloader.domain.job_snapshot import JobSnapshot

#: Was eine Oberflaeche mindestens braucht. Namentlich aufgefuehrt statt
#: abgezaehlt, damit ein fehlendes Feld beim Namen genannt wird.
REQUIRED_FIELDS = {
    "id", "url", "title", "state", "progress", "done", "total", "unit",
    "has_known_total", "output_file", "expected_bytes", "error", "from_disk", "seq",
}


def a_running_job(tmp_path: Path) -> DownloadJob:
    """Ein Job mit allem daran, was nicht transportierbar ist."""
    job = DownloadJob(
        url="https://provider.test/x", quality="best", output_dir=tmp_path
    )
    job.title = "Ein Video"
    job.output_file = tmp_path / "Ein Video.mp4"
    job.progress_unit = ProgressUnit.BYTES
    job.update_progress(512, 2048)
    job.transition(LifecycleState.DOWNLOADING)
    job.expected_bytes = 2048
    job.asyncio_task = object()

    async def confirm(_job, _size: int) -> bool:
        return True

    job.confirm_large_download = confirm
    return job


def test_a_snapshot_carries_the_state_a_view_needs(tmp_path):
    job = a_running_job(tmp_path)

    snapshot = JobSnapshot.of(job)

    assert {field.name for field in fields(snapshot)} >= REQUIRED_FIELDS
    assert snapshot.id == job.id
    assert snapshot.url == job.url
    assert snapshot.title == "Ein Video"
    assert snapshot.state is LifecycleState.DOWNLOADING
    assert (snapshot.done, snapshot.total) == (512, 2048)
    assert snapshot.unit is ProgressUnit.BYTES
    assert snapshot.has_known_total is True
    assert snapshot.progress == 25.0
    assert snapshot.output_file == job.output_file
    assert snapshot.expected_bytes == 2048
    assert snapshot.from_disk is False
    assert snapshot.seq == job.seq


def test_a_snapshot_carries_no_runtime_objects(tmp_path):
    """Weder Task noch Event noch Rueckruf - und auch keine Ausnahme."""
    job = a_running_job(tmp_path)
    job.error = JobError(
        kind=ErrorKind.TRACK,
        code="TrackDownloadError",
        message="die Audiospur kam nicht an",
        retryable=True,
        cause=RuntimeError("die echte Ursache"),
    )

    snapshot = JobSnapshot.of(job)

    for field in fields(snapshot):
        value = getattr(snapshot, field.name)
        assert not callable(value), f"{field.name} traegt ein Callable"
        assert not isinstance(value, (asyncio.Event, asyncio.Task)), field.name
        assert not isinstance(value, BaseException), field.name

    assert snapshot.error is not None
    assert snapshot.error.cause is None, "die Ursache gehoert ins Log, nicht in den Schnappschuss"
    # Der Job selbst behaelt sie - sie ist dort das, was das Log braucht.
    assert isinstance(job.error.cause, RuntimeError)


def test_a_snapshot_does_not_change_when_the_job_moves_on(tmp_path):
    """Eingefroren zu sein ist die Eigenschaft, die ihn brauchbar macht."""
    job = a_running_job(tmp_path)
    taken = JobSnapshot.of(job)

    job.update_progress(2048, 2048)
    job.mark_completed()

    assert taken.state is LifecycleState.DOWNLOADING
    assert taken.done == 512
    assert JobSnapshot.of(job).state is LifecycleState.COMPLETED


def test_a_snapshot_without_a_failure_carries_none(tmp_path):
    assert JobSnapshot.of(a_running_job(tmp_path)).error is None


def test_a_disk_entry_says_that_it_is_one(tmp_path):
    """Fuer so einen Eintrag bedeutet Entfernen das Loeschen einer echten Datei."""
    job = DownloadJob(
        url="", quality="best", output_dir=tmp_path, title="alt.mp4", from_disk=True
    )
    job.output_file = tmp_path / "alt.mp4"
    job.mark_completed()

    snapshot = JobSnapshot.of(job)

    assert snapshot.from_disk is True
    assert snapshot.state is LifecycleState.COMPLETED
    assert snapshot.progress == 100.0


def test_the_error_text_is_the_sentence_it_always_was(tmp_path):
    """Tooltip, Logzeilen und mehrere Tests lesen diesen Text."""
    error = JobError(
        kind=ErrorKind.REFUSAL,
        code="XUnavailableError",
        message="Der Beitrag ist nicht verfuegbar.",
        retryable=False,
    )

    assert str(error) == "XUnavailableError: Der Beitrag ist nicht verfuegbar."

    without_code = JobError(
        kind=ErrorKind.INCOMPLETE, code="", message="Download blieb unvollständig.",
        retryable=True,
    )
    assert str(without_code) == "Download blieb unvollständig."
