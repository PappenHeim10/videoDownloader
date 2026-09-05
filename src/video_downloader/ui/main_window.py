from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPushButton, QProgressBar, QVBoxLayout, QWidget,
)

from video_downloader.domain.download_job import DownloadJob, LifecycleState, ProgressUnit
from video_downloader.application.download_manager import DownloadManager
from video_downloader.infrastructure.settings import AppSettings


class JobBridge(QObject):
    changed = Signal(object)


#: Bytes are shown in MiB - the 1024-based unit, named as such, because a
#: number labelled "MB" that is computed with 1024 is the more common lie.
_BYTES_PER_MIB = 1024 * 1024


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
    def __init__(self, job: DownloadJob, manager: DownloadManager, delete_callback, parent=None):
        super().__init__(parent)
        self.job = job
        self.manager = manager
        self._closing = False
        self.bridge = JobBridge(self)
        self.bridge.changed.connect(self.refresh)
        job.on_change = self.bridge.changed.emit
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.title = QLabel()
        remove = QPushButton("X")
        remove.setFixedWidth(32)
        remove.clicked.connect(lambda: asyncio.create_task(delete_callback(job)))
        header.addWidget(self.title)
        header.addWidget(remove)
        layout.addLayout(header)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.refresh(job)

    def mouseReleaseEvent(self, event):
        if self.job.state == LifecycleState.COMPLETED and self.job.output_file:
            if self.job.output_file.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.job.output_file)))
            else:
                self.status.setText("Datei nicht gefunden")
        super().mouseReleaseEvent(event)

    def refresh(self, job: DownloadJob):
        self.title.setText(job.title or job.url or "Vorhandene Datei")
        self.status.setText(f"{job.state.value} {progress_details(job)}".strip())
        if job.state == LifecycleState.DOWNLOADING and not job.has_known_total:
            # A server that states no length gives a total of 0 for the whole
            # run. Qt's own indeterminate mode says "running, end unknown"; a
            # bar parked at 0 % would claim we know it has not started.
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(round(job.progress))
        self.status.setToolTip(job.error or "")


class MainWindow(QMainWindow):
    def __init__(self, manager: DownloadManager, settings: AppSettings | None = None):
        super().__init__()
        self.manager = manager
        self.settings = settings or AppSettings()
        self._closing = False
        self._shutdown_done = False
        self.setWindowTitle("Video Downloader")

        folder_menu = self.menuBar().addMenu("&Einstellungen")
        folder_menu.addAction("Download-Ordner ändern…", self.change_download_directory)
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
        """The directory the next job should use, asking the user if needed.

        A stored directory that no longer exists counts as unset - AppSettings
        reports it as absent - so we ask again rather than inventing a fallback
        next to the executable or in the working directory.
        """
        configured = self.settings.get_download_directory()
        if configured is not None:
            return configured

        chosen = self._ask_for_directory("Zielordner für Downloads wählen")
        if chosen is None:
            return None
        return self.settings.set_download_directory(chosen)

    def change_download_directory(self) -> Path | None:
        chosen = self._ask_for_directory("Neuen Zielordner für Downloads wählen")
        if chosen is None:
            return None
        # Future jobs only. Running and finished jobs keep the directory they
        # were created with, so nothing moves under the user's feet.
        return self.settings.set_download_directory(chosen)

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
            url, self.quality.text().strip() or "best", output_dir=directory
        )
        self.add_item(job)
        self.url.clear()

    def add_item(self, job: DownloadJob):
        item = QListWidgetItem(self.list)
        widget = DownloadItem(job, self.manager, self.delete_item)
        item.setSizeHint(widget.sizeHint())
        self.list.setItemWidget(item, widget)

    async def delete_item(self, job: DownloadJob):
        await self.manager.delete_download(job)
        for index in range(self.list.count()):
            item = self.list.item(index)
            widget = self.list.itemWidget(item)
            if widget is not None and widget.job is job:
                self.list.takeItem(index)
                break

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