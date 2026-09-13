"""Der darstellbare Zustand eines Jobs, losgeloest vom Job selbst.

`DownloadJob` ist ein laufendes Ding: es haelt den `asyncio.Task`, der es
ausfuehrt, das `asyncio.Event`, mit dem es gestoppt wird, und die Rueckrufe, mit
denen es Fragen stellt. Eine Oberflaeche braucht nichts davon - sie braucht,
was anzuzeigen ist. Bislang gab es diese Trennung nicht: das Fenster las die
Felder der Dataclass direkt, und damit auch die drei, die kein Konsument je
sehen duerfte.

Ein `JobSnapshot` ist deshalb genau das, was uebrig bleibt, wenn man die
Laufzeitobjekte weglaesst - nicht mehr, und vor allem nicht weniger:
`test_job_snapshot.py` prueft beide Richtungen, damit ein neues Feld am Job
nicht stillschweigend aus dem Schnappschuss faellt.

Der Wert ist eingefroren und wird bei jedem `of()` neu gebildet. Das ist die
Eigenschaft, die ihn brauchbar macht: er veraendert sich nicht mehr, waehrend
ein Leser ihn liest.

`seq` ist die Reihenfolge, in der die Aenderungen am Job angewendet wurden.
Zwei Schnappschuesse desselben Jobs sind daran vergleichbar, auch wenn sie ueber
verschiedene Wege bei einem Leser ankommen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from video_downloader.domain.job_error import JobError
from video_downloader.domain.download_job import LifecycleState, ProgressUnit

if TYPE_CHECKING:  # pragma: no cover - nur fuer die Typpruefung
    from video_downloader.domain.download_job import DownloadJob


@dataclass(frozen=True)
class JobSnapshot:
    """Was ein Job gerade ist, ohne das, womit er es ist."""

    id: str
    url: str
    title: str
    state: LifecycleState
    #: 0-100. Aussagekraeftig nur, wenn `has_known_total` gilt.
    progress: float
    #: Erledigte und erwartete Menge in der Einheit `unit`. Die Felder heissen
    #: am Job noch `downloaded_segments`/`total_segments`; hier nicht mehr, weil
    #: sie bei einem progressiven Download Bytes zaehlen.
    done: int
    total: int
    unit: ProgressUnit
    #: Ob `total` ueberhaupt etwas behauptet. `False` heisst "Ende unbekannt",
    #: nicht "nichts zu tun".
    has_known_total: bool
    output_file: Path | None
    expected_bytes: int | None
    error: JobError | None
    #: Ob dieser Eintrag aus einem Verzeichnisscan stammt statt aus einem
    #: Download dieser Sitzung. Die Unterscheidung ist noetig, weil dasselbe
    #: Bedienelement sonst zwei sehr verschiedene Dinge tut.
    from_disk: bool
    seq: int

    @classmethod
    def of(cls, job: DownloadJob) -> JobSnapshot:
        return cls(
            id=job.id,
            url=job.url,
            title=job.title,
            state=job.state,
            progress=job.progress,
            done=job.downloaded_segments,
            total=job.total_segments,
            unit=job.progress_unit,
            has_known_total=job.has_known_total,
            output_file=job.output_file,
            expected_bytes=job.expected_bytes,
            # Die Ursache ist ein Laufzeitobjekt und bleibt am Job.
            error=job.error.without_cause() if job.error is not None else None,
            from_disk=job.from_disk,
            seq=job.seq,
        )
