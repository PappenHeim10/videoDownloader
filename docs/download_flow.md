# Code-Analyse: Download-Ablauf und Komponenten

Diese Dokumentation beschreibt die Architektur und den genauen Ablauf eines Video-Downloads in der Anwendung, basierend auf der Analyse der Code-Basis.

## 1. Hauptkomponenten

Die Anwendung ist modular aufgebaut und trennt die Benutzeroberfläche (bzw. CLI) strikt von der Download-Logik.

*   **`DownloadJob` (`download_job.py`)**: 
    Eine Datenklasse (Data Class), die den vollständigen Zustand eines einzelnen Downloads kapselt. Sie enthält Metadaten (URL, Qualität, Titel), den aktuellen `LifecycleState` (z.B. `QUEUED`, `DOWNLOADING`, `COMPLETED`), Fortschrittsdaten sowie ein `asyncio.Event` (`stop_event`) für den sicheren Abbruch des asynchronen Tasks.
*   **`DownloadManager` (`download_manager.py`)**: 
    Verwaltet die Liste aller Downloads (`DownloadJob`s). Er kontrolliert die Start-, Stopp- und Löschvorgänge der Jobs. Optional kann er die maximale Anzahl gleichzeitiger Downloads über ein `asyncio.Semaphore` limitieren.
*   **`run_download_job` (`download_service.py`)**: 
    Die asynchrone Kernfunktion, die den tatsächlichen Download-Prozess steuert. Sie kennt keine Website: sie holt sich eine `ProviderSession`, lässt die `ProviderRegistry` die URL zu einem `Media` auflösen und übergibt dessen HLS-`MediaSource` an `BaseCore.download(...)`.
*   **`ProviderSession` (`provider_session.py`)**: 
    Die Provider-Ressourcen genau eines Jobs - Registry plus der `BaseCore`, mit dem der Job herunterlädt. Die Adapter besitzen ihre eigenen Extraktions-Clients (deren Session-Header und Cookies bleiben dort); der Download-Core bleibt provider-sauber. Zusammengesetzt wird die Session in `bootstrap.create_provider_session`, geschlossen genau einmal von dem Job, der sie erzeugt hat.
*   **Einstiegspunkte (`main.py` / `downoader.py`)**: 
    `main.py` startet die Qt-basierte Benutzeroberfläche (`PySide6`) gekoppelt mit einem `qasync` Event-Loop, während `downoader.py` (CLI) den Download direkt in der Konsole über die Standard-`asyncio`-Schleife ausführt.

## 2. Der genaue Download-Ablauf

Der Download eines Videos durchläuft mehrere wohldefinierte Phasen (Lifecycles):

1.  **Job-Erstellung (State: `CREATED` $\rightarrow$ `QUEUED`)**:
    *   Der Benutzer startet einen Download (über UI oder CLI).
    *   `DownloadManager.add_download()` wird mit der URL, Qualität und der Remux-Option aufgerufen.
    *   Ein neues `DownloadJob`-Objekt wird erstellt und in das interne Dictionary des Managers eingefügt. Der Status wechselt auf `QUEUED`.
    *   Der Manager ruft `start_download()` auf, wodurch ein `asyncio.Task` für den Job erzeugt wird.

2.  **Verbindungsaufbau (State: `CONNECTING`)**:
    *   Der `asyncio.Task` führt die Funktion `run_download_job()` aus.
    *   Der Status wechselt zu `CONNECTING`. Die injizierte Factory erzeugt die `ProviderSession` dieses Jobs (eigene Registry mit adapter-eigenen Extraktions-Clients, eigener provider-sauberer Download-`BaseCore`).

3.  **Metadaten abrufen (State: `FETCHING_METADATA`)**:
    *   Der Status wechselt auf `FETCHING_METADATA`.
    *   Mit `await session.registry.resolve(url)` wählt die Registry den passenden Provider und liefert ein `Media` (Titel, Autoren, `MediaSource`-Liste). Jede `MediaSource` trägt ihre eigenen Transport-Header (`MediaSource.headers`, z. B. den xHamster-`Referer`); eine direkte `.m3u8`-Quelle trägt keine.
    *   Passt kein Provider (`UnsupportedURLError`) oder passen mehrere (`AmbiguousProviderError`), endet der Job als `FAILED`, ohne dass ein Download versucht wird - im Log getrennt von Netz-, Extraktions- und Segmentfehlern.
    *   Der Ausgabepfad (`job.output_file`) wird auf Basis des Titels generiert (`{Titel}.mp4`) und genau so an die Engine weitergereicht.

4.  **Download-Phase (State: `DOWNLOADING`)**:
    *   Der Status wechselt auf `DOWNLOADING`.
    *   Eine interne `callback`-Funktion wird definiert. Diese Funktion empfängt die Download-Fortschritte (heruntergeladene vs. gesamte Segmente) und aktualisiert den Job. Um die UI nicht zu überlasten (UI-Freeze zu verhindern), werden die UI-Updates im Callback auf maximal alle 0.15 Sekunden limitiert ("coalescing").
    *   Aus dem `Media` wird die HLS-`MediaSource` gewählt (sonst `UnsupportedProtocolError`) und `session.core.download(DownloadConfigHLS(...))` aufgerufen. Hier fließen die Qualitätsstufe, der Speicherpfad, die Callback-Funktion, die Remux-Einstellung, der Resume-State-Pfad und das `stop_event` des Jobs (für Abbrüche) ein. Die Header der `MediaSource` wendet `BaseCore` pro Request an - auf Master-Manifest, Media-Playlist und jedes Segment inkl. Retries; sie überschreiben Session-Header nur für den jeweiligen Request und verändern die Session nie.
    *   Die Segmente des Transportstreams (TS) werden heruntergeladen und, falls konfiguriert, zu einer MP4-Datei geremuxt.

5.  **Abschluss & Aufräumen (State: `COMPLETED` / `CANCELLED` / `FAILED`)**:
    *   Wurde das `stop_event` während des Downloads gesetzt (Abbruch durch User), wechselt der Status zu `CANCELLED`.
    *   Tritt ein Fehler auf oder gibt der Download einen Fehler zurück, wird der Status auf `FAILED` gesetzt und die Fehlermeldung in `job.error` hinterlegt.
    *   Ist der Download erfolgreich, wird der Status auf `COMPLETED` gesetzt, der Fortschritt auf 100% korrigiert und temporäre State-Dateien (`.state/{id}.json`) werden gelöscht.
    *   Im `finally`-Block wird die `ProviderSession` des Jobs geschlossen (`await session.close()`) - genau einmal, auch bei Fehler und Abbruch.

## 3. Besonderheiten der Architektur

*   **Asynchrone Programmierung:** Die gesamte Download- und Netzwerklogik ist mit `asyncio` implementiert. In der GUI-Version (`main.py`) wird `qasync` verwendet, um die Qt-Event-Schleife mit `asyncio` zu verheiraten, sodass UI-Updates reibungslos funktionieren.
*   **Zustands-Tracking (State Files):** Downloads erstellen eine Datei in einem `.state`-Unterordner. Dies ermöglicht potenziell die Wiederaufnahme (Resuming) von unvollständigen Downloads nach einem App-Absturz.
*   **Event-getriebene Updates:** Die `DownloadJob`-Klasse ruft eine `on_change`-Methode auf, sobald sich Zustände oder der Fortschritt ändern. Dies ist ein Observer-Muster, wodurch die UI sofort reagieren kann, wenn sich im Backend etwas ändert.
