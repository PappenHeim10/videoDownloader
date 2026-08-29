from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable


class LifecycleState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    CONNECTING = "connecting"
    FETCHING_METADATA = "fetching_metadata"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class DownloadJob:
    url: str
    quality: str | int
    output_dir: Path
    remux: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    title: str = ""
    output_file: Path | None = None
    state: LifecycleState = LifecycleState.CREATED
    downloaded_segments: int = 0
    total_segments: int = 0
    progress: float = 0.0
    error: str | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    asyncio_task: object | None = None
    on_change: Callable[["DownloadJob"], None] | None = field(default=None, repr=False)

    @property
    def state_file(self) -> Path:
        return self.output_dir / ".state" / f"{self.id}.json"

    def transition(self, state: LifecycleState) -> None:
        self.state = state
        self._notify()

    def update_progress(self, downloaded: int, total: int) -> None:
        self.downloaded_segments = downloaded
        self.total_segments = total
        self.progress = downloaded / total * 100 if total else 0.0
        self._notify()

    def request_stop(self) -> None:
        self.stop_event.set()
        self._notify()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change(self)