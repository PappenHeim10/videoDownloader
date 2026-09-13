"""Eine Ausnahme in die Kategorie uebersetzen, die sie schon hat.

Die Einordnung wird hier nicht erfunden. Jede Kategorie unten entspricht einem
Ast der Ausnahmehierarchie, den es bereits gibt und den `run_download_job` schon
heute getrennt behandelt - es hat das Ergebnis nur nicht aufgehoben, sondern
sofort zu einem Satz zusammengefaltet.

Die Reihenfolge der Pruefungen ist die einzige Stelle mit echtem Inhalt: sie
geht von der speziellsten Aussage zur allgemeinsten. `ProviderLoginRequired`
steht vor `ProviderRefusal`, weil es eine ist - und die einzige, auf die zu
reagieren sich lohnt.

Getrennt von `domain.job_error`, weil nur diese Schicht die Provider- und
Engine-Ausnahmen kennen darf; der Wert selbst kennt keine davon.
"""

from __future__ import annotations

from base_api.modules.errors import (
    AmbiguousProviderError,
    UnsupportedProtocolError,
    UnsupportedURLError,
)

from video_downloader.application.muxing import MuxError
from video_downloader.application.provider_refusal import (
    ProviderLoginRequired,
    ProviderRefusal,
)
from video_downloader.application.provider_session import ProviderNotConfiguredError
from video_downloader.application.track_download import (
    DownloadTooLargeError,
    TrackDownloadError,
)
from video_downloader.domain.job_error import ErrorKind, JobError

#: Kategorie und Wiederholbarkeit je Ausnahmeast, von speziell nach allgemein.
#: Ein Tupel statt eines Dicts, weil die Reihenfolge Teil der Aussage ist:
#: `ProviderLoginRequired` ist ein `ProviderRefusal`, und wer zuerst passt,
#: gewinnt.
_RULES: tuple[tuple[type[BaseException], ErrorKind, bool], ...] = (
    # Eine Anmeldung wuerde daraus einen Download machen - das ist die eine
    # Ablehnung, die ein erneuter Versuch aufloesen kann.
    (ProviderLoginRequired, ErrorKind.LOGIN_REQUIRED, True),
    # Ein gesperrtes Konto, ein geloeschter Beitrag, ein Live-Stream: die
    # Antwort ist fuer jeden dieselbe und bleibt es.
    (ProviderRefusal, ErrorKind.REFUSAL, False),
    # Der Benutzer hat nein gesagt. Ihn ungefragt erneut zu fragen, waere die
    # Frage zu missachten.
    (DownloadTooLargeError, ErrorKind.TOO_LARGE, False),
    # Kein oder mehr als ein Provider. Nichts wurde geholt, nichts geschrieben.
    ((UnsupportedURLError, AmbiguousProviderError), ErrorKind.SELECTION, False),
    # Ein Provider hat geantwortet, bietet aber nichts Herunterladbares an.
    (UnsupportedProtocolError, ErrorKind.UNSUPPORTED, False),
    # Eine Spur fehlt. Die andere kann fertig sein, und ein erneuter Versuch
    # kostet dann nur diese eine.
    (TrackDownloadError, ErrorKind.TRACK, True),
    # Die Bytes liegen vollstaendig auf der Platte. Ein erneuter Versuch kostet
    # keinen einzigen davon.
    (MuxError, ErrorKind.MUX, True),
    # Kein Schreibrecht, kein Platz, eine gesperrte Datei - das aendert sich,
    # wenn der Benutzer etwas dagegen tut.
    (OSError, ErrorKind.FILESYSTEM, True),
    # Niemand hat die Komposition verdrahtet. Ein Programmierfehler, und ein
    # erneuter Versuch aendert daran nichts.
    (ProviderNotConfiguredError, ErrorKind.UNKNOWN, False),
)


def classify(error: BaseException) -> JobError:
    """Die Ausnahme als Wert, mit genau dem Satz, der vorher in `job.error` stand."""
    for types, kind, retryable in _RULES:
        if isinstance(error, types):
            return JobError(
                kind=kind,
                code=type(error).__name__,
                message=str(error),
                retryable=retryable,
                cause=error,
            )

    # Unbekannt heisst unbekannt. Als wiederholbar gefuehrt, weil ein Fehler,
    # den wir nicht einordnen koennen, haeufiger ein abgerissenes Netz ist als
    # eine dauerhafte Absage - und ein erneuter Versuch kostet hier weniger als
    # eine falsche Endgueltigkeit.
    return JobError(
        kind=ErrorKind.UNKNOWN,
        code=type(error).__name__,
        message=str(error),
        retryable=True,
        cause=error,
    )
