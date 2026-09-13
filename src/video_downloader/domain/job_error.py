"""Warum ein Job fehlgeschlagen ist, als Wert statt als Satz.

Bis hierher wurde jede Ausnahme sofort zu `f"{type(error).__name__}: {error}"`
zusammengefaltet. Der Satz stimmte - er ist genau das, was die Oberflaeche
anzeigt - aber alles, was ein Aufrufer daraus haette ableiten koennen, war
danach weg: ob ein erneuter Versuch ueberhaupt eine Chance hat, ob eine
Anmeldung fehlte, ob die Bytes bereits auf der Platte liegen und nur das
Zusammenfuegen scheiterte.

Diese Unterscheidungen existieren bereits - als Ausnahmehierarchie
(`ProviderRefusal`, `ProviderLoginRequired`, `TrackDownloadError`, `MuxError`,
`DownloadTooLargeError` und die Auswahlfehler der Registry). Sie werden hier
nicht erfunden, sondern nur aufgehoben; die Zuordnung selbst steht in
`application.job_failure`, weil nur diese Schicht die Hierarchie kennen darf.

Zwei Entscheidungen, die den Typ erklaeren:

* **`__str__` reproduziert den bisherigen Satz Zeichen fuer Zeichen.** Der
  Tooltip, die Logzeilen und mehrere Tests lesen diesen Text; ein Umbau der
  Struktur ist kein Grund, die Formulierung zu aendern.
* **`cause` ist nicht Teil des Wertes.** Es haengt fuer das Log daran, nimmt
  aber an Vergleich und `repr` nicht teil und faellt aus jedem Schnappschuss
  heraus (`without_cause`). Eine Ausnahme ist ein Laufzeitobjekt, und ein
  Schnappschuss, der eines mitfuehrt, ist keiner.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class ErrorKind(StrEnum):
    """Die Art des Fehlschlags - das, worauf ein Aufrufer verschieden reagiert.

    Bewusst grob: jede Kategorie beantwortet eine andere Frage des Benutzers,
    und eine, die keine eigene Antwort hat, ist keine eigene Kategorie.
    """

    #: Die Registry fand keinen oder mehr als einen Provider. Nichts wurde
    #: geholt, nichts geschrieben.
    SELECTION = "selection"
    #: Ein Provider wurde gefunden, bietet aber nichts an, was diese Anwendung
    #: herunterladen kann.
    UNSUPPORTED = "unsupported"
    #: Der Provider hat geantwortet, und die Antwort war nein.
    REFUSAL = "refusal"
    #: Eine Ablehnung, die eine Anmeldung in einen Download verwandeln wuerde.
    LOGIN_REQUIRED = "login_required"
    #: Der Benutzer hat den Download wegen seiner Groesse abgelehnt.
    TOO_LARGE = "too_large"
    #: Eine einzelne Spur kam nicht an. Die andere kann fertig sein.
    TRACK = "track"
    #: Die Bytes liegen vollstaendig auf der Platte, das Zusammenfuegen schlug fehl.
    MUX = "mux"
    #: Schreiben, Verschieben, Loeschen - das Dateisystem hat nein gesagt.
    FILESYSTEM = "filesystem"
    #: Der Download endete unvollstaendig, ohne dass eine Ausnahme geworfen wurde.
    INCOMPLETE = "incomplete"
    #: Alles andere. Ein Fehler ohne Einordnung ist keiner mit falscher.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobError:
    """Ein Fehlschlag, so wie ihn eine Oberflaeche und ein Log brauchen."""

    kind: ErrorKind
    #: Der Name der Ausnahmeklasse, oder leer, wenn keine geworfen wurde.
    code: str
    message: str
    #: Ob ein erneuter Versuch ueberhaupt etwas anderes ergeben kann. Eine
    #: geloeschte Datei antwortet jedem dasselbe; ein abgerissenes Netz nicht.
    retryable: bool
    #: Nur fuers Log. Nicht Teil der Gleichheit, nicht im `repr`, nie in einem
    #: Schnappschuss.
    cause: BaseException | None = field(default=None, repr=False, compare=False)

    def __str__(self) -> str:
        """Genau der Satz, der vorher in `job.error` stand."""
        return f"{self.code}: {self.message}" if self.code else self.message

    def without_cause(self) -> JobError:
        """Derselbe Fehler, ohne das Laufzeitobjekt daran."""
        return self if self.cause is None else replace(self, cause=None)
