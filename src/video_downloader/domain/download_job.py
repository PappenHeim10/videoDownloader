from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable

from video_downloader.domain.job_error import ErrorKind, JobError

logger = logging.getLogger(__name__)


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


#: Was ein Beobachter eines Jobs ist. Absichtlich der Job selbst und nicht ein
#: Schnappschuss: der bisherige Vertrag, und ein Beobachter, der einen
#: Schnappschuss will, bildet ihn sich mit `JobSnapshot.of(job)`.
JobListener = Callable[["DownloadJob"], None]


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
    #: Warum der Job fehlgeschlagen ist, eingeordnet statt zusammengefaltet.
    #: `str(job.error)` liefert weiterhin genau den Satz von frueher.
    error: JobError | None = None
    #: Ob dieser Eintrag aus einem Verzeichnisscan stammt statt aus einem
    #: Download. Fuer so einen Eintrag bedeutet "entfernen" das Loeschen einer
    #: echten Videodatei, und das darf ein Aufrufer unterscheiden koennen.
    from_disk: bool = False
    #: Bytes this job is expected to transfer, once the tracks are known. `None`
    #: until then, and for a provider that states no size.
    expected_bytes: int | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    #: Asked before a large download starts, with the estimated total in bytes.
    #: `None` means nobody is there to ask - a CLI or a test - and the download
    #: proceeds. Answering False cancels it before a byte is transferred.
    #:
    #: Awaited rather than called: die Frage wird von einer Oberflaeche
    #: beantwortet, und die darf dafuer nicht den Loop anhalten, auf dem die
    #: anderen Downloads laufen.
    confirm_large_download: Callable[["DownloadJob", int], Awaitable[bool]] | None = field(
        default=None, repr=False
    )
    #: Asked when a provider refuses the URL only because nobody is signed in to
    #: that site. Awaited rather than called: the login happens on the site's own
    #: page in a window, and the loop has to keep running - other downloads keep
    #: going while it is open. The argument is the refusal itself, which carries
    #: the site, its login page and the cookies that mean it worked.
    #:
    #: `None` means nobody is there to ask - a CLI, a test - and the refusal
    #: stands. Returning True means a session now exists and the resolution is
    #: worth exactly one more attempt.
    request_login: Callable[[Exception], Awaitable[bool]] | None = field(
        default=None, repr=False
    )
    asyncio_task: object | None = None

    #: Die Reihenfolge, in der Aenderungen angewendet wurden. Monoton, nur unter
    #: `_apply` erhoeht, und damit der einzige verlaessliche Vergleich zwischen
    #: zwei Beobachtungen desselben Jobs.
    seq: int = field(default=0, init=False)

    _listeners: list[JobListener] = field(
        default_factory=list, init=False, repr=False, compare=False
    )
    #: Der Loop, auf dem Aenderungen angewendet werden. Siehe `bind_loop`.
    _loop: asyncio.AbstractEventLoop | None = field(
        default=None, init=False, repr=False, compare=False
    )
    #: Wie viele Aenderungen noch in der Warteschlange des Loops stehen. Solange
    #: das nicht null ist, reiht sich auch eine Aenderung vom Loop-Thread ein -
    #: sonst ueberholte sie die aelteren und ein Beobachter saehe COMPLETED vor
    #: dem letzten Fortschritt.
    _pending: int = field(default=0, init=False, repr=False, compare=False)
    _pending_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    # --- Zustandsfragen ----------------------------------------------------

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

    @property
    def is_finished(self) -> bool:
        return self.state in (
            LifecycleState.COMPLETED,
            LifecycleState.CANCELLED,
            LifecycleState.FAILED,
        )

    # --- Beobachter --------------------------------------------------------

    def add_listener(self, listener: JobListener) -> None:
        """Register `listener` for every change to this job.

        Registriert, nicht zugewiesen. Der Unterschied ist der Grund fuer die
        Methode: solange es ein einzelnes `on_change`-Feld gab, machte ein
        zweiter Beobachter den ersten stumm, ohne dass irgendwo etwas
        fehlschlug.

        Doppelte Registrierung desselben Callables ist wirkungslos - sonst
        haette ein versehentlich zweimal aufgebautes Widget jede Aenderung
        doppelt bekommen.
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: JobListener) -> None:
        """Stop notifying `listener`. Unbekannte Beobachter sind kein Fehler.

        Nachsichtig, weil der Aufrufer genau an der Stelle abmeldet, an der er
        ohnehin aufraeumt - und ein Aufraeumpfad, der wegen einer doppelten
        Abmeldung wirft, ist schlechter als eine, die nichts tut.
        """
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    @property
    def listener_count(self) -> int:
        """Wie viele Beobachter angemeldet sind. Fuer Tests und Diagnose."""
        return len(self._listeners)

    # --- Anwenden von Aenderungen ------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Nenne den Loop, auf dem Aenderungen dieses Jobs angewendet werden.

        Der Grund ist gemessen und nicht theoretisch: Fortschritt erreicht einen
        Job aus Worker-Threads - der yt-dlp-Hook laeuft in `asyncio.to_thread`,
        das Muxen ebenso, und der Remux der Engine auch. Bisher hat die
        Qt-Signalverbindung das aufgefangen, weil sie ueber Threadgrenzen hinweg
        zu einer Queued Connection wird. Das ist eine Eigenschaft der
        Oberflaeche, nicht des Kerns, und faellt mit ihr weg.

        Ohne gebundenen Loop - ein reiner Einheitstest - wird direkt angewendet.
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._loop = loop

    def _on_bound_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _apply(self, mutate: Callable[[], None]) -> None:
        """Wende eine Aenderung an und benachrichtige - in genau einer Reihenfolge.

        Drei Faelle, und der mittlere ist der, um den es geht:

        * kein Loop gebunden -> direkt, wie bisher.
        * auf dem Loop-Thread **und** nichts haengt -> direkt, damit ein
          `transition()` unmittelbar danach sichtbar ist.
        * sonst -> eingereiht. Das gilt auch fuer den Loop-Thread selbst,
          solange noch etwas aus einem Worker haengt: sonst ueberholt die
          neuere Aenderung die aeltere.
        """
        if self._loop is None:
            mutate()
            self._notify()
            return

        # Die Sperre schuetzt nur den Zaehler und wird vor dem Anwenden wieder
        # abgegeben. Sonst liefe `_notify()` unter ihr, und ein Beobachter, der
        # den Job dabei anfasst, stuende auf einer nicht wiedereintrittsfaehigen
        # Sperre still.
        with self._pending_lock:
            immediately = self._pending == 0 and self._on_bound_loop()
            if not immediately:
                self._pending += 1

        if immediately:
            mutate()
            self._notify()
            return

        try:
            self._loop.call_soon_threadsafe(self._apply_queued, mutate)
        except RuntimeError:
            # Der Loop ist zu, waehrend noch ein Worker meldet. Die Aenderung
            # ist dann nichts mehr wert, aber der Zaehler muss stimmen.
            with self._pending_lock:
                self._pending -= 1

    def _apply_queued(self, mutate: Callable[[], None]) -> None:
        with self._pending_lock:
            self._pending -= 1
        mutate()
        self._notify()

    def _notify(self) -> None:
        """Alle Beobachter, ueber eine Kopie, und keiner reisst die anderen mit.

        Die Kopie erlaubt einem Beobachter, sich waehrend der Benachrichtigung
        abzumelden. Der Schutzblock ist die zweite Haelfte derselben Regel: ein
        Fehler in einer Oberflaeche ist ihr Fehler, und darf einen laufenden
        Download nicht anhalten.
        """
        self.seq += 1
        for listener in tuple(self._listeners):
            try:
                listener(self)
            except Exception:  # noqa: BLE001 - ein Beobachter darf nichts mitreissen
                logger.exception(
                    "[JOB %s] Ein Beobachter hat beim Benachrichtigen geworfen", self.id
                )

    # --- Zustandsaenderungen ----------------------------------------------

    def transition(self, state: LifecycleState) -> None:
        def mutate() -> None:
            self.state = state

        self._apply(mutate)

    def update_progress(self, downloaded: int, total: int) -> None:
        def mutate() -> None:
            self.downloaded_segments = downloaded
            self.total_segments = total
            self.progress = downloaded / total * 100 if total else 0.0

        self._apply(mutate)

    def mark_completed(self) -> None:
        """Der Job ist fertig - und sagt das auch, wenn ihn nie jemand gewogen hat.

        Der Fall, den das behebt: ein Track, dessen Provider keine Groesse nennt
        und fuer den der Resolver weder `total_bytes` noch eine Schaetzung
        liefert. Dann blieb der Gesamtwert null, die Korrektur auf 100 % lief
        unter `if job.total_segments` ins Leere, und ein fertiger Download stand
        auf 0 %.

        Der Gesamtwert wird auf das gesetzt, was tatsaechlich ankam - das ist
        keine Erfindung, sondern die einzige Zahl, die am Ende belegt ist.

        Bewusst als Zustandsaenderung des Jobs und nicht im Aufrufer: so laeuft
        sie durch dieselbe Warteschlange wie der Fortschritt und liest dessen
        Endwert, statt ihn womoeglich zu ueberholen.
        """

        def mutate() -> None:
            if self.total_segments <= 0 and self.downloaded_segments > 0:
                self.total_segments = self.downloaded_segments
            self.progress = 100.0
            self.state = LifecycleState.COMPLETED

        self._apply(mutate)

    def fail(self, error: JobError) -> None:
        def mutate() -> None:
            self.error = error
            self.state = LifecycleState.FAILED

        self._apply(mutate)

    def request_stop(self) -> None:
        # Sofort, nicht eingereiht: ein Abbruch, der auf den naechsten
        # Loop-Durchlauf wartet, laesst die Worker weiterladen.
        self.stop_event.set()
        self._apply(lambda: None)

    # --- Hilfen ------------------------------------------------------------

    def failed_with(
        self,
        kind: ErrorKind,
        message: str,
        *,
        code: str = "",
        retryable: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        """Kurzform fuer `fail(JobError(...))` an den Stellen ohne Ausnahme."""
        self.fail(
            JobError(
                kind=kind, code=code, message=message, retryable=retryable, cause=cause
            )
        )
