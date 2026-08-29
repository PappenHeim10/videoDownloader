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
               +--> ProviderSession (pro Job, aus bootstrap.create_provider_session)
                      |
                      +--> ProviderRegistry.resolve(url) -> Media (+ MediaSource.headers)
                      |      +--> XHamsterAdapter   (xhamster.com/.desi, eigener Extraktions-Client)
                      |      +--> DirectMediaAdapter (direkte .m3u8-URLs, keine Header)
                      |
                      +--> base_api.BaseCore.download(DownloadConfigHLS)   [provider-sauberer Core]
                             +--> Manifest-/Playlist-/Segment-Requests mit MediaSource.headers
                             +--> paralleler Segment-Download
                             +--> Stop-Event, Resume-State, Remux
```

### Startschicht: `__main__.py` und `debug_main.py`

Diese Skripte importieren `run_application` aus `bootstrap.py`. `debug_main.py` startet mit aktiviertem Debugging, einem Watchdog für UI-Freeze-Detection und ausführlichen Logs. `__main__.py` startet die Produktionsversion.

### Anwendungsschicht: `DownloadManager` und `DownloadJob`

Der `DownloadManager` orchestriert alle aktiven `DownloadJob`-Instanzen. Ein Job wird asynchron via `run_download_job` ausgefuehrt. Der Manager kontrolliert die maximale Anzahl gleichzeitiger Downloads über ein `asyncio.Semaphore(max_concurrent_downloads=3)`.

### UI-Schicht: `MainWindow`

`MainWindow` nutzt PySide6. Es laedt regelmässig den Status aus den `DownloadJob`s und aktualisiert die ProgressBar und Label.

### Provider-Schicht: `ProviderRegistry` und Adapter

Die Registry waehlt anhand von `supports(url)` genau einen Provider aus und liefert dessen `resolve(url)` als provider-neutrales `Media` mit `MediaSource`-Liste zurueck. Passt keiner, kommt `UnsupportedURLError`; passen mehrere, `AmbiguousProviderError`. Registriert sind `XHamsterAdapter` (laedt die HTML-Seite und extrahiert die M3U8-URL) und `DirectMediaAdapter` (rein URL-basiert, ohne Netzwerk).

Zwei Transport-Kontexte sind bewusst getrennt:

* **Provider-Session (Extraktion)**: Jeder Adapter besitzt seinen eigenen Client. Was dessen Session an Zustand ansammelt - der xHamster-`Referer`, Cookies aus dem Seitenabruf - bleibt dort und endet mit `registry.close()`.
* **`MediaSource.headers` (Medien-Download)**: Was die Medien-Requests einer Quelle mitschicken muessen, steht an der Quelle selbst (z. B. `Referer` fuer Hotlink-Schutz) und wird von `BaseCore` pro Request angewendet - auf Master-Manifest, Media-Playlist und jedes Segment inkl. Retries. Der Download-Core des Jobs bleibt provider-sauber; kein Adapter schreibt je auf seine Session. Praezedenz: Quell-Header schlagen Session-Header fuer genau diesen Request (case-insensitiv); die Session selbst wird nie veraendert. Eine direkte `.m3u8`-Quelle erbt dadurch keinen fremden `Referer` mehr.

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
2. **Provider-Isolierung**: Fuer jeden `DownloadJob` erzeugt die injizierte Factory (`bootstrap.create_provider_session`) eine eigene `ProviderSession` - eigene Registry mit adapter-eigenen Extraktions-Clients plus ein provider-sauberer Download-`BaseCore` - und `run_download_job` schliesst sie im `finally`, auch bei Fehler und Abbruch. Ein gemeinsam genutztes Registry wuerde entweder pro Job die Session anderer laufender Jobs schliessen oder die bisherige Isolation aufgeben. Medien-Transport-Header gehoeren auf `MediaSource.headers`, nie auf die Session des Download-Cores.
3. **Neue Website**: nur in `bootstrap.create_provider_session` registrieren. Der Download-Workflow kennt keine Website, sondern nur `Media`/`MediaSource`.
4. **Abbruch**: Ueber `job.cancel()` wird das `stop_event` gesetzt. Das HLS-Backend beendet sich geordnet.
