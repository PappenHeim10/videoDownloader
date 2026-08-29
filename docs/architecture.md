# Architekturhilfe des Video-Downloaders

Stand: 2026-08-28

## Zweck der App

Die App ist ein asynchroner PySide6/qasync-Desktop-Downloader fuer HLS-Videos. Sie nimmt Video-URLs entgegen, laedt die Videometadaten, ermittelt eine M3U8-Playlist und laedt deren Segmente parallel herunter. Optional werden die geladenen Transport-Stream-Daten anschliessend in eine MP4-Datei remuxt. Multi-Download wird durch den `DownloadManager` unterstuetzt.

Der Anwendungscode ist strukturiert im Verzeichnis `src/video_downloader`. Die eigentliche HTTP-, HLS- und Remux-Logik kommt aus den lokalen Paketen `xhamster-api` und `base-api` (unter `packages/`).

## Komponenten

```text
src/video_downloader/__main__.py / debug_main.py
        |
        v
    bootstrap.py (configure_logging, exception_handlers, qasync EventLoop)
        |
        v
    DownloadManager (Verwaltet mehrere DownloadJobs, Semaphore für Concurrency)
        |
        v
    MainWindow (UI: Fortschrittsbalken, URL-Eingabe, Status-Updates)
        |
        +--> DownloadJob (Datenklasse fuer jeden aktiven Download)
        |
        +--> run_download_job (Isoliert in download_service.py)
               |
               +--> xhamster_api.Client
                      |
                      +--> get_video()
                      +--> Video.download()
                             |
                             +--> base_api.BaseCore (HTTP-Requests, HLS-Segmente)
                                    +--> paralleler Download
                                    +--> Stop-Event via asyncio.to_thread
```

### Startschicht: `__main__.py` und `debug_main.py`

Diese Skripte importieren `run_application` aus `bootstrap.py`. `debug_main.py` startet mit aktiviertem Debugging, einem Watchdog für UI-Freeze-Detection und ausführlichen Logs. `__main__.py` startet die Produktionsversion.

### Anwendungsschicht: `DownloadManager` und `DownloadJob`

Der `DownloadManager` orchestriert alle aktiven `DownloadJob`-Instanzen. Ein Job wird asynchron via `run_download_job` ausgefuehrt. Der Manager kontrolliert die maximale Anzahl gleichzeitiger Downloads über ein `asyncio.Semaphore(max_concurrent_downloads=3)`.

### UI-Schicht: `MainWindow`

`MainWindow` nutzt PySide6. Es laedt regelmässig den Status aus den `DownloadJob`s und aktualisiert die ProgressBar und Label.

### Adapter-Schicht: `packages/xhamster-api`

Der `Client` liefert ein `Video`-Objekt. Das `Video`-Objekt laedt die HTML-Seite und extrahiert daraus die M3U8-URL.

### Download-Schicht: `packages/base-api`

`BaseCore` uebernimmt die technischen Aufgaben (HTTP-Requests, HLS-Playlist, Segment-Download, Remuxing). Die Schnittstelle unterstuetzt ein `threading.Event`, welches im asynchronen PySide6-Umfeld per `asyncio.to_thread(stop_event.wait)` ueberwacht wird, um ein UI-Freeze zu verhindern.

## Lebenszyklus und Zustandsautomat

Jeder `DownloadJob` verwendet `LifecycleState`:

```text
QUEUED
  -> RUNNING (Fetch Metadata -> Downloading)
  -> COMPLETED

Oder Abbruch/Fehler:
  -> CANCELLED
  -> FAILED
```

## Fehler- und Exit-Code-Modell

Ein GUI-Crash wirft eine Exception, die vom qasync Exception Handler oder dem allgemeinen `sys.excepthook` in der Logdatei (`runtime/logs/downloader.log`) protokolliert wird.

## Wartungsregeln

1. **Kein blockierender Code im Main-Thread**: `threading.Event().wait()` oder Netzwerk-Requests duerfen niemals direkt im QEventLoop ausgefuehrt werden (nutze `asyncio.to_thread` oder `aiohttp`).
2. **Client Isolierung**: Fuer jeden `DownloadJob` wird in `run_download_job` eine frische `Client`-Instanz und ein eigener `BaseCore` erzeugt, um state/sessions (wie `core.session` oder cookies) pro Download zu trennen.
3. **Abbruch**: Ueber `job.cancel()` wird das `stop_event` gesetzt. Das HLS-Backend beendet sich geordnet.
