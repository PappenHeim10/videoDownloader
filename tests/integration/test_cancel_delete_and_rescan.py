"""Abbrechen, Entfernen und der Blick in den Zielordner - drei Dinge, kein Knopf.

Bis hierher gab es aus Sicht einer Oberflaeche nur eines: `delete_download`
brach ab, loeschte die Ausgabedatei und raeumte den Eintrag weg, in einem Zug
und ohne Frage. `cancel_download` existierte und war von nirgends erreichbar.

Die Trennung ist der Punkt dieser Datei:

* **Abbrechen** stoppt den Lauf und laesst liegen, was auf der Platte ist -
  einschliesslich des Resume-States, der einen spaeteren Versuch billig macht.
* **Entfernen** nimmt den Eintrag weg; ob die Datei mitgeht, sagt der Aufrufer.
  `delete_file` hat deshalb keine Vorgabe.

Dazu der Ordnerwechsel: der Verzeichnisscan lief genau einmal, im Konstruktor.
Wer danach den Zielordner wechselte, sah weiter die Dateien des alten.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from video_downloader.application.download_manager import DownloadManager
from video_downloader.domain.download_job import DownloadJob, LifecycleState


async def _runs_until_stopped(job: DownloadJob) -> DownloadJob:
    """Ein Job, der laeuft, bis ihn jemand stoppt - wie ein echter Download."""
    job.transition(LifecycleState.DOWNLOADING)
    await job.stop_event.wait()
    job.transition(LifecycleState.CANCELLED)
    return job


def a_manager(directory: Path) -> DownloadManager:
    return DownloadManager(directory, job_runner=_runs_until_stopped)


def a_finished_file(directory: Path, name: str = "fertig.mp4") -> Path:
    path = directory / name
    path.write_bytes(b"video")
    return path


# --- Abbrechen --------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_stops_the_job_and_keeps_its_file(tmp_path):
    """Der Grund fuer die Trennung: eine fertige Datei gehoert dem Benutzer."""
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)
    output = a_finished_file(tmp_path, "halbfertig.mp4")
    job.output_file = output

    await manager.cancel_download(job)

    assert job.state is LifecycleState.CANCELLED
    assert output.exists(), "Abbrechen hat die Datei geloescht"
    assert job in manager.get_jobs(), "Abbrechen hat den Eintrag entfernt"


@pytest.mark.asyncio
async def test_cancel_keeps_the_resume_state(tmp_path):
    """Halb geladene Bytes sind genau das, was einen Neustart billig macht."""
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)
    job.state_file.parent.mkdir(parents=True, exist_ok=True)
    job.state_file.write_text("{}", encoding="utf-8")

    await manager.cancel_download(job)

    assert job.state_file.exists()


@pytest.mark.asyncio
async def test_cancelling_a_finished_job_does_nothing(tmp_path):
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)
    await manager.cancel_download(job)

    await manager.cancel_download(job)  # ein zweites Mal

    assert job.state is LifecycleState.CANCELLED


# --- Entfernen --------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_without_the_file_keeps_it_on_disk(tmp_path):
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)
    output = a_finished_file(tmp_path)
    job.output_file = output

    await manager.delete_download(job, delete_file=False)

    assert output.exists()
    assert job not in manager.get_jobs()


@pytest.mark.asyncio
async def test_delete_with_the_file_removes_it(tmp_path):
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)
    output = a_finished_file(tmp_path)
    job.output_file = output

    await manager.delete_download(job, delete_file=True)

    assert not output.exists()
    assert job not in manager.get_jobs()


@pytest.mark.asyncio
async def test_delete_always_clears_the_resume_state(tmp_path):
    """Der Job ist weg; sein auf die ID ausgestellter State waere verwaist."""
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)
    job.state_file.parent.mkdir(parents=True, exist_ok=True)
    job.state_file.write_text("{}", encoding="utf-8")

    await manager.delete_download(job, delete_file=False)

    assert not job.state_file.exists()


def test_delete_file_has_to_be_said_out_loud(tmp_path):
    """Ohne Vorgabewert, damit niemand das Loeschen versehentlich erbt."""
    import inspect

    parameter = inspect.signature(DownloadManager.delete_download).parameters["delete_file"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# --- Ordnerwechsel ----------------------------------------------------------


def test_the_first_scan_finds_what_is_already_there(tmp_path):
    a_finished_file(tmp_path, "alt.mp4")

    manager = DownloadManager(tmp_path, job_runner=_runs_until_stopped)

    found = manager.get_jobs()
    assert [job.title for job in found] == ["alt.mp4"]
    assert found[0].from_disk is True
    assert found[0].state is LifecycleState.COMPLETED
    assert found[0].progress == 100.0


def test_a_rescan_after_a_folder_change_shows_the_new_folder(tmp_path):
    """Der Fall, der vorher nicht vorgesehen war."""
    first = tmp_path / "erster"
    first.mkdir()
    a_finished_file(first, "alt.mp4")
    second = tmp_path / "zweiter"
    second.mkdir()
    a_finished_file(second, "neu.mp4")

    manager = DownloadManager(first, job_runner=_runs_until_stopped)
    assert [job.title for job in manager.get_jobs()] == ["alt.mp4"]

    manager.rescan_output_directory(second)

    assert [job.title for job in manager.get_jobs()] == ["neu.mp4"]
    assert manager.output_dir == second.resolve()


@pytest.mark.asyncio
async def test_a_rescan_leaves_this_sessions_jobs_alone(tmp_path):
    """Ein laufender Download gehoert dem Benutzer, nicht dem Verzeichnisscan."""
    manager = a_manager(tmp_path)
    job = manager.add_download("https://provider.test/x")
    await asyncio.sleep(0)

    manager.rescan_output_directory()

    assert job in manager.get_jobs()
    await manager.cancel_download(job)


def test_a_rescan_does_not_duplicate_what_it_already_found(tmp_path):
    a_finished_file(tmp_path, "alt.mp4")
    manager = DownloadManager(tmp_path, job_runner=_runs_until_stopped)

    manager.rescan_output_directory()
    manager.rescan_output_directory()

    assert [job.title for job in manager.get_jobs()] == ["alt.mp4"]


def test_a_rescan_picks_up_a_file_that_appeared_since(tmp_path):
    manager = DownloadManager(tmp_path, job_runner=_runs_until_stopped)
    assert manager.get_jobs() == []

    a_finished_file(tmp_path, "spaeter.webm")
    manager.rescan_output_directory()

    assert [job.title for job in manager.get_jobs()] == ["spaeter.webm"]
