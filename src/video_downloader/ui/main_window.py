from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QProgressBar, QVBoxLayout, QWidget,
)

from video_downloader.domain.download_job import DownloadJob, LifecycleState, ProgressUnit
from video_downloader.application.download_directory import DownloadDirectory
from video_downloader.application.download_manager import DownloadManager
from video_downloader.infrastructure.session_store import SessionStore
from video_downloader.infrastructure.settings import AppSettings
# The one site with a login, named here only for the menu entry that offers it
# ahead of time. The automatic path never names a provider: it is driven by the
# refusal, which carries its own site, login page and cookie names.
from video_downloader.providers.x import XLoginRequiredError


class JobBridge(QObject):
    changed = Signal(object)


#: Bytes are shown in MiB - the 1024-based unit, named as such, because a
#: number labelled "MB" that is computed with 1024 is the more common lie.
_BYTES_PER_MIB = 1024 * 1024


#: What each lifecycle state is called in the window. Only the states a user
#: needs a word for are listed; the rest keep the enum's own value, which is
#: already readable.
_STATE_WORDING = {
    LifecycleState.MUXING: "Spuren werden zusammengefuegt",
}


def state_wording(job: DownloadJob) -> str:
    """The state in the user's words.

    Muxing needs one badly: without it the bar sits full, no file exists yet,
    and nothing on screen explains the wait.
    """
    return _STATE_WORDING.get(job.state, job.state.value)


def progress_details(job: DownloadJob) -> str:
    """The counters, in the words of whatever unit the job is counting in.

    A progressive download whose total is still unknown says how much has
    arrived and nothing about how much is left - "0 von 0" and a frozen
    percentage would both be claims we cannot make.
    """
    if job.progress_unit is ProgressUnit.BYTES:
        if not job.total_segments and not job.downloaded_segments:
            return ""
        done = job.downloaded_segments / _BYTES_PER_MIB
        if not job.total_segments:
            return f"{done:.1f} MiB"
        return f"{done:.1f} / {job.total_segments / _BYTES_PER_MIB:.1f} MiB"
    return (
        f"{job.downloaded_segments} / {job.total_segments} Segmente"
        if job.total_segments
        else ""
    )


class DownloadItem(QFrame):
    def __init__(
        self,
        job: DownloadJob,
        manager: DownloadManager,
        delete_callback,
        cancel_callback=None,
        parent=None,
    ):
        super().__init__(parent)
        self.job = job
        self.manager = manager
        self._closing = False
        self.bridge = JobBridge(self)
        self.bridge.changed.connect(self.refresh)
        # Registriert statt zugewiesen. Der Unterschied ist nicht kosmetisch:
        # solange es ein einzelnes Feld war, machte ein zweites Widget fuer
        # denselben Job das erste stumm, und `detach()` unten haette nichts
        # gehabt, woran es sich abmelden koennte.
        job.add_listener(self._job_changed)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.title = QLabel()
        cancel = QPushButton("Abbrechen")
        cancel.setToolTip("Den Download stoppen. Die Datei bleibt liegen.")
        cancel.clicked.connect(
            lambda: asyncio.create_task((cancel_callback or self._cancel)(job))
        )
        self.cancel_button = cancel
        remove = QPushButton("X")
        remove.setFixedWidth(32)
        remove.setToolTip("Den Eintrag und seine Datei entfernen.")
        remove.clicked.connect(lambda: asyncio.create_task(delete_callback(job)))
        header.addWidget(self.title)
        header.addWidget(cancel)
        header.addWidget(remove)
        layout.addLayout(header)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.refresh(job)

    def _job_changed(self, job: DownloadJob) -> None:
        """Der Uebergang in den Qt-Thread.

        Die Signalverbindung ist hier absichtlich weiterhin im Spiel, obwohl der
        Kern seine Aenderungen inzwischen selbst auf dem Loop serialisiert: sie
        kostet nichts und haelt das Fenster auch dann korrekt, wenn ein Job
        ohne gebundenen Loop benachrichtigt - ein Einheitstest etwa.
        """
        self.bridge.changed.emit(job)

    def detach(self) -> None:
        """Diesen Eintrag vom Job abmelden.

        Ohne das blieb das Widget als Beobachter haengen, nachdem es aus der
        Liste genommen war - eine Benachrichtigung danach lief in ein Objekt,
        dessen C++-Haelfte Qt bereits geloescht haben konnte.
        """
        self.job.remove_listener(self._job_changed)

    async def _cancel(self, job: DownloadJob) -> None:
        await self.manager.cancel_download(job)

    def mouseReleaseEvent(self, event):
        if self.job.state == LifecycleState.COMPLETED and self.job.output_file:
            if self.job.output_file.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.job.output_file)))
            else:
                self.status.setText("Datei nicht gefunden")
        super().mouseReleaseEvent(event)

    def refresh(self, job: DownloadJob):
        self.title.setText(job.title or job.url or "Vorhandene Datei")
        self.status.setText(f"{state_wording(job)} {progress_details(job)}".strip())
        if job.state == LifecycleState.DOWNLOADING and not job.has_known_total:
            # A server that states no length gives a total of 0 for the whole
            # run. Qt's own indeterminate mode says "running, end unknown"; a
            # bar parked at 0 % would claim we know it has not started.
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(round(job.progress))
        self.status.setToolTip(str(job.error) if job.error is not None else "")
        # Ein Eintrag aus dem Verzeichnisscan laeuft nicht, und ein fertiger
        # Job auch nicht - beiden waere mit "Abbrechen" nicht gedient.
        self.cancel_button.setVisible(not job.from_disk and not job.is_finished)


class MainWindow(QMainWindow):
    def __init__(
        self,
        manager: DownloadManager,
        settings: AppSettings | None = None,
        sessions: SessionStore | None = None,
    ):
        super().__init__()
        self.manager = manager
        self.settings = settings or AppSettings()
        # The rule for where downloads land lives in the application layer;
        # this window only supplies the folder picker it asks with.
        self.directory = DownloadDirectory(self.settings)
        # The same store the job registries read from, handed in by the
        # composition root. `None` means no login is on offer - a test, or a
        # window built without one - and the actions below say so rather than
        # pretending.
        self.sessions = sessions
        self._closing = False
        self._shutdown_done = False
        self.setWindowTitle("Video Downloader")

        folder_menu = self.menuBar().addMenu("&Einstellungen")
        folder_menu.addAction("Download-Ordner ändern…", self.change_download_directory)
        folder_menu.addSeparator()
        folder_menu.addAction(
            f"Bei {XLoginRequiredError.site} anmelden…",
            lambda: self._run(self.sign_in_to_site(XLoginRequiredError)),
        )
        folder_menu.addAction(
            f"Von {XLoginRequiredError.site} abmelden",
            lambda: self.sign_out_of_site(XLoginRequiredError.site),
        )
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("Video URL")
        self.quality = QLineEdit("best")
        download = QPushButton("Download")
        download.clicked.connect(self.add_download)
        row.addWidget(self.url, 1)
        row.addWidget(self.quality)
        row.addWidget(download)
        layout.addLayout(row)
        self.list = QListWidget()
        layout.addWidget(self.list)
        for job in manager.get_jobs():
            self.add_item(job)

    def _ask_for_directory(self, title: str) -> Path | None:
        """Open the folder picker. Returns None when the user cancels.

        Separate method so tests can drive the flow without a real dialog.
        """
        chosen = QFileDialog.getExistingDirectory(self, title)
        return Path(chosen) if chosen else None

    def resolve_download_directory(self) -> Path | None:
        """The directory the next job should use, asking through the picker."""
        return self.directory.resolve(self._ask_for_directory)

    def change_download_directory(self) -> Path | None:
        """Pick a new destination, then make the list describe it."""
        directory = self.directory.change(self._ask_for_directory)
        if directory is None:
            return None
        # The list showed the files of the old folder until here - it described
        # a directory nothing is written to any more.
        self.manager.rescan_output_directory(directory)
        self.rebuild_list()
        return directory

    def _run(self, coroutine) -> None:
        """Start a coroutine a menu entry asked for.

        A menu action is a plain callable and cannot await, so the work goes to
        the loop the window already runs on. Nothing waits for the result: the
        window reports what happened itself.
        """
        asyncio.ensure_future(coroutine)

    async def sign_in_to_site(self, refusal) -> bool:
        """Show a site's own login page and keep the session it produces.

        Takes the refusal - or the refusal *class*, which is what the menu entry
        has - because both carry the same three facts: the site, its login page,
        and the cookies that mean the login worked. Nothing here knows which
        provider raised it.
        """
        if self.sessions is None:
            self.statusBar().showMessage(
                "Anmeldung ist in diesem Fenster nicht verfuegbar.", 5000
            )
            return False

        # Imported here and nowhere else: this is what pulls in Qt WebEngine,
        # and a session that never signs in never pays for it.
        from video_downloader.ui.login_window import sign_in

        session = await sign_in(
            site=refusal.site,
            login_url=refusal.login_url,
            required_cookies=refusal.required_cookies,
            parent=self,
        )
        if session is None:
            self.statusBar().showMessage(
                f"Anmeldung bei {refusal.site} abgebrochen.", 5000
            )
            return False

        kept = self.sessions.save(session)
        self.statusBar().showMessage(
            f"Bei {session.site} angemeldet."
            if kept
            else f"Bei {session.site} angemeldet - gilt nur fuer diese Sitzung.",
            5000,
        )
        return True

    def sign_out_of_site(self, site: str) -> None:
        """Forget a site's session, here and on disk."""
        if self.sessions is None:
            return
        removed = self.sessions.clear(site)
        self.statusBar().showMessage(
            f"Von {site} abgemeldet."
            if removed
            else f"Es war keine Anmeldung fuer {site} gespeichert.",
            5000,
        )

    def add_download(self):
        url = self.url.text().strip()
        if not url:
            return

        directory = self.resolve_download_directory()
        if directory is None:
            # Cancelling the picker is an ordinary decision, not an error: no job
            # is created, nothing is written, and the window stays usable.
            self.statusBar().showMessage("Download abgebrochen: kein Zielordner gewählt.", 5000)
            return

        job = self.manager.add_download(
            url,
            self.quality.text().strip() or "best",
            output_dir=directory,
            confirm_large_download=self.confirm_large_download,
            request_login=self.sign_in_to_site,
        )
        self.add_item(job)
        self.url.clear()

    async def confirm_large_download(self, job: DownloadJob, estimated_bytes: int) -> bool:
        """Ask before starting a download large enough to be worth a question.

        Asked once, before the first byte, because that is the only moment the
        answer can still save anything. The size is the provider's estimate, so
        the wording says "about" rather than pretending to a precision it does
        not have.

        Gezeigt statt `exec`-t, aus demselben Grund wie das Anmeldefenster: ein
        `exec` fuehrt eine geschachtelte Qt-Schleife, und waehrend jemand
        ueberlegt, sollen die anderen Downloads weiterlaufen und ihren
        Fortschritt melden. Wird der fragende Job in der Zwischenzeit
        abgebrochen, raeumt das `finally` das Fenster weg, statt es
        herrenlos stehen zu lassen.
        """
        gib = estimated_bytes / 1024 ** 3
        box = QMessageBox(self)
        box.setWindowTitle("Grosser Download")
        box.setText(
            f"""„{job.title or job.url}“ ist etwa {gib:.1f} GiB gross.

Fortfahren?"""
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)

        answer: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        def settle(result: int) -> None:
            if not answer.done():
                answer.set_result(result)

        box.finished.connect(settle)
        box.open()
        try:
            # Ein Fenster, das ohne Antwort geschlossen wird, liefert 0 - und
            # damit dasselbe wie Nein, was die Vorbelegung ohnehin war.
            confirmed = await answer == int(QMessageBox.StandardButton.Yes)
        finally:
            box.close()
            box.deleteLater()

        if not confirmed:
            self.statusBar().showMessage(
                f"Download abgebrochen: {gib:.1f} GiB waren zu viel.", 5000
            )
        return confirmed

    def add_item(self, job: DownloadJob):
        item = QListWidgetItem(self.list)
        widget = DownloadItem(job, self.manager, self.delete_item)
        item.setSizeHint(widget.sizeHint())
        self.list.setItemWidget(item, widget)

    def take_item(self, job: DownloadJob) -> None:
        """Nimm den Eintrag dieses Jobs aus der Liste - und melde ihn ab."""
        for index in range(self.list.count()):
            item = self.list.item(index)
            widget = self.list.itemWidget(item)
            if widget is not None and widget.job is job:
                widget.detach()
                self.list.takeItem(index)
                break

    def rebuild_list(self) -> None:
        """Baue die Liste aus dem auf, was der Manager jetzt fuehrt.

        Jeder bestehende Eintrag meldet sich vorher ab. Ohne das haette ein
        Neuaufbau genau den Beobachter-Ueberhang erzeugt, den `detach()`
        verhindern soll - nur eben fuer die ganze Liste auf einmal.
        """
        for index in range(self.list.count()):
            widget = self.list.itemWidget(self.list.item(index))
            if widget is not None:
                widget.detach()
        self.list.clear()
        for job in self.manager.get_jobs():
            self.add_item(job)

    async def delete_item(self, job: DownloadJob):
        # `delete_file=True` ist genau das bisherige Verhalten dieses Knopfes.
        # Es steht jetzt hier statt in der Vorbelegung des Managers, damit die
        # Entscheidung an der Stelle sichtbar ist, an der sie getroffen wird.
        await self.manager.delete_download(job, delete_file=True)
        self.take_item(job)

    def closeEvent(self, event):
        if self._shutdown_done:
            event.accept()
            return
        if self._closing:
            event.ignore()
            return
        event.ignore()
        self._closing = True
        asyncio.create_task(self._shutdown())

    async def _shutdown(self):
        await self.manager.shutdown()
        self._shutdown_done = True
        self.close()