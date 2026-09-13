"""Ein Listeneintrag meldet sich an - und wieder ab.

Der Befund, der diese Datei ausgeloest hat: `delete_item` nahm den Eintrag aus
der Liste, setzte den Beobachter am Job aber nicht zurueck. Eine Meldung danach
lief in ein Widget, dessen C++-Haelfte Qt bereits geloescht haben konnte. In der
Praxis selten, weil vorher abgebrochen wird - aber ein letzter Callback aus
einem Worker-Thread nach der Rueckkehr von `cancel_download` war nicht
ausgeschlossen.

Geprueft wird an `listener_count`, nicht an einem Absturz: ob Qt das Objekt
schon eingezogen hat, ist nicht deterministisch, die Anmeldung am Job aber
schon.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from video_downloader.application.download_manager import DownloadManager
from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.infrastructure.paths import HOME_ENV_VAR
from video_downloader.infrastructure.settings import AppSettings
from video_downloader.ui.main_window import MainWindow


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    manager = MagicMock(spec=DownloadManager)
    manager.get_jobs.return_value = []
    manager.delete_download = AsyncMock(return_value=None)
    manager.cancel_download = AsyncMock(return_value=None)
    return MainWindow(manager, AppSettings())


def a_job(tmp_path: Path) -> DownloadJob:
    return DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)


def test_an_entry_registers_exactly_one_listener(window, tmp_path):
    job = a_job(tmp_path)

    window.add_item(job)

    assert job.listener_count == 1


@pytest.mark.asyncio
async def test_removing_an_entry_detaches_it_from_the_job(window, tmp_path):
    """Der Ueberhang, den es vorher gab."""
    job = a_job(tmp_path)
    window.add_item(job)

    await window.delete_item(job)

    assert job.listener_count == 0, "der entfernte Eintrag hoert dem Job weiter zu"
    assert window.list.count() == 0


@pytest.mark.asyncio
async def test_a_detached_entry_survives_a_later_change(window, tmp_path):
    """Nach dem Entfernen darf eine spaete Meldung nirgendwo mehr ankommen."""
    job = a_job(tmp_path)
    window.add_item(job)
    await window.delete_item(job)

    job.transition(LifecycleState.CANCELLED)  # darf nicht werfen

    assert job.state is LifecycleState.CANCELLED


def test_rebuilding_the_list_detaches_every_previous_entry(window, tmp_path):
    """Ein Neuaufbau wuerde sonst denselben Ueberhang fuer die ganze Liste erzeugen."""
    old = [a_job(tmp_path) for _ in range(3)]
    for job in old:
        window.add_item(job)
    assert all(job.listener_count == 1 for job in old)

    fresh = a_job(tmp_path)
    window.manager.get_jobs.return_value = [fresh]
    window.rebuild_list()

    assert all(job.listener_count == 0 for job in old)
    assert fresh.listener_count == 1
    assert window.list.count() == 1


@pytest.mark.asyncio
async def test_the_remove_button_still_deletes_the_file(window, tmp_path):
    """Das bisherige Verhalten dieses Knopfes, jetzt ausdruecklich gesagt."""
    job = a_job(tmp_path)
    window.add_item(job)

    await window.delete_item(job)

    window.manager.delete_download.assert_awaited_once_with(job, delete_file=True)


def test_changing_the_folder_rescans_and_rebuilds(window, tmp_path):
    """Die Liste zeigte sonst weiter die Dateien des alten Ordners."""
    target = tmp_path / "neu"
    target.mkdir()
    window._ask_for_directory = lambda title: target

    window.change_download_directory()

    window.manager.rescan_output_directory.assert_called_once()
    assert window.settings.get_download_directory() == target.resolve()
