"""Turning a job's changes into messages, without the job knowing there is a wire.

A job notifies listeners; this attaches one per job and forwards what it hears.
Everything that makes that safe already lives in the job: the listener list, the
loop it serialises its own changes on, and the guard that a listener which
raises cannot stop a download. So this module is deliberately thin - if it were
doing anything clever, that would be a sign the cleverness belongs one layer
down.

The one piece of bookkeeping it does own is which jobs it is already attached
to. Jobs arrive from three directions - added by the front end, found by a
directory scan, present at startup - and attaching twice would be harmless only
because the job deduplicates listeners. Tracking it here means `detach_all` can
actually leave nothing behind.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from video_downloader.domain.download_job import DownloadJob
from video_downloader.host import protocol

logger = logging.getLogger(__name__)


class JobEventPublisher:
    """Attaches to jobs and turns every change into a `job.changed` message."""

    def __init__(self, send: Callable[[dict[str, Any]], bool]) -> None:
        self._send = send
        self._attached: dict[str, DownloadJob] = {}

    @property
    def attached_count(self) -> int:
        return len(self._attached)

    def attach(self, job: DownloadJob, *, announce: bool = False) -> None:
        """Watch this job. `announce` also sends it as newly created.

        The distinction matters for the front end: a job it asked for is new to
        it, and a job that was already running when it connected is not.
        """
        if job.id not in self._attached:
            self._attached[job.id] = job
            job.add_listener(self._changed)
        if announce:
            self._send({"type": protocol.JOB_CREATED, "job": protocol.job_payload(job)})

    def attach_all(self, jobs: list[DownloadJob]) -> None:
        for job in jobs:
            self.attach(job)

    def detach(self, job: DownloadJob, *, announce: bool = False) -> None:
        if self._attached.pop(job.id, None) is not None:
            job.remove_listener(self._changed)
        if announce:
            self._send({"type": protocol.JOB_REMOVED, "jobId": job.id})

    def detach_all(self) -> None:
        """Let go of every job. Called when the front end disconnects.

        Without this the core would keep serialising snapshots for a reader that
        no longer exists - cheap, but it is exactly the observer overhang that
        the listener model was built to make impossible.
        """
        for job in list(self._attached.values()):
            job.remove_listener(self._changed)
        self._attached.clear()

    def forget(self, job_id: str) -> None:
        """Stop watching a job that is already gone from the manager.

        The rescan is the case: it drops the entries of the previous directory
        outright, so there is no job object left to detach from.
        """
        job = self._attached.pop(job_id, None)
        if job is not None:
            job.remove_listener(self._changed)

    def announce_removed(self, job_id: str) -> None:
        self._send({"type": protocol.JOB_REMOVED, "jobId": job_id})

    def _changed(self, job: DownloadJob) -> None:
        self._send({"type": protocol.JOB_CHANGED, "job": protocol.job_payload(job)})
