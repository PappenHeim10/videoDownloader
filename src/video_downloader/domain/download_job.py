from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable


class ProgressUnit(StrEnum):
    """What a job's progress counters count.

    The counters themselves keep their names and their meaning: a segmented
    HLS download still reports segments in `downloaded_segments` /
    `total_segments`, which is what every existing caller and test reads. A
    progressive HTTP download puts bytes in the same pair, and this is how a
    reader knows which of the two it is looking at - "4 / 12" and
    "4194304 / 12582912" need very different words in front of a user.
    """

    SEGMENTS = "segments"
    BYTES = "bytes"


class LifecycleState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    CONNECTING = "connecting"
    FETCHING_METADATA = "fetching_metadata"
    DOWNLOADING = "downloading"
    #: Both tracks are on disk and are being combined into one file. Its own
    #: state because otherwise the progress bar sits at 100% with no file to
    #: open and nothing to explain the wait.
    MUXING = "muxing"
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
    #: Segments unless a transport says otherwise, so a job built by any
    #: existing caller keeps exactly the semantics it had.
    progress_unit: ProgressUnit = ProgressUnit.SEGMENTS
    progress: float = 0.0
    error: str | None = None
    #: Bytes this job is expected to transfer, once the tracks are known. `None`
    #: until then, and for a provider that states no size.
    expected_bytes: int | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: Asked before a large download starts, with the estimated total in bytes.
    #: `None` means nobody is there to ask - a CLI or a test - and the download
    #: proceeds. Returning False cancels it before a byte is transferred.
    confirm_large_download: Callable[["DownloadJob", int], bool] | None = field(
        default=None, repr=False
    )
    asyncio_task: object | None = None
    on_change: Callable[["DownloadJob"], None] | None = field(default=None, repr=False)

    @property
    def state_file(self) -> Path:
        return self.output_dir / ".state" / f"{self.id}.json"

    @property
    def has_known_total(self) -> bool:
        """Whether `progress` means anything yet.

        A progressive download over a server that states no length reports a
        total of 0 for its whole run - deliberately, because inventing a
        denominator would put a moving percentage on an unknown end. The UI
        needs to be able to tell that apart from "0 of 0 done".
        """
        return self.total_segments > 0

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