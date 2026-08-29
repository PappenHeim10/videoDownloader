# Architektur-Analyse des Video-Downloaders

Stand: 2026-08-27

## 1. Kurzueberblick

`downoader.py` ist ein asynchroner HLS-Video-Downloader. Das Skript verwendet `xhamster_api` als Website-Adapter und `eaf-base-api` als Netzwerk-, Cache- und Download-Engine.

Der aktuell installierte Versionsstand ist wichtig:

- `xhamster_api 2.2`
- `eaf-base-api 4.1.1`
- Python 3.13

Diese Versionen verwenden teilweise unterschiedliche API-Generationen. Deshalb wurden im installierten Paket Kompatibilitaetsanpassungen benoetigt:

- `video_constructor` wird in `Helper` als Alias fuer `constructor` akzeptiert.
- `core.fetch(...)` wurde durch `core.fetch_text(...)` ersetzt.
- `Video.download(...)` baut jetzt ein `DownloadConfigHLS`-Objekt und uebergibt es an `BaseCore.download(...)`.
- `setup_logger(...)` wurde als Adapter auf `configure_app_logging(...)` definiert.

Die Anpassungen liegen momentan unter `Python313\\Lib\\site-packages`. Eine Neuinstallation der Pakete kann sie ueberschreiben.

## 2. Was macht der Downloader?

Der Ablauf in `downoader.py` ist:

1. `asyncio.run(main())` startet den asynchronen Event Loop.
2. Die Video-URL wird festgelegt.
3. Der Zielordner `Videos\\downloads` wird erzeugt.
4. Ein `Client` wird erstellt.
5. Der Client ruft die HTML-Seite mit `BaseCore.fetch_text(url)` ab.
6. `Video.init()` speichert den HTML-Inhalt im `Video`-Objekt.
7. Eigenschaften wie Titel und M3U8-URL werden bei Bedarf aus dem HTML extrahiert.
8. `Video.download(...)` erstellt `DownloadConfigHLS`.
9. `BaseCore.download(...)` liest die M3U8-Playlist.
10. Die Playlist wird in einzelne HLS-Segmente aufgeloest.
11. Die Segmente werden parallel heruntergeladen.
12. Die Segmente werden im Speicher oder im Segmentordner zusammengefuehrt.
13. Bei `remux=True` werden die Transport-Stream-Daten mit PyAV/FFmpeg-kompatibler Logik in eine MP4-Datei umgewandelt.
14. Die Statusdatei `downloads\\xhamster.state.json` ermoeglicht das Fortsetzen eines unterbrochenen Downloads.
15. Der Callback gibt den Fortschritt auf der Konsole aus.

Das Skript laedt keine Datei ueber einen einzelnen direkten MP4-Link herunter. Es laedt eine HLS-Playlist und viele kleinere Segmente.

## 3. Beteiligte Klassen und Funktionen

### Anwendung

- `main()` in `downoader.py`: Orchestriert den gesamten Ablauf.
- `custom_callback(downloaded, total)`: Gibt den Fortschritt aus.
- `Path`: Erzeugt und verwaltet den absoluten Zielordner.
- `threading.Event`: Ist als Abbruchsignal vorgesehen.
- `traceback.print_exc()`: Gibt bei einem Fehler den vollstaendigen Traceback aus.

### xhamster_api

- `Client`: Oeffentliche API fuer `get_video(...)`.
- `Video`: Haltet URL und HTML-Daten und extrahiert Titel und M3U8-URL.
- `Video.init()`: Laedt die Videoseite.
- `Video.download(...)`: Uebersetzt die alte oeffentliche Download-Signatur in `DownloadConfigHLS`.
- `get_html_content(...)`: Laedt HTML ueber `fetch_text(...)` und wandelt bestimmte Fehler um.
- `setup_logger(...)`: Adapter auf die Logger-Funktion des Basispakets.
- `Something`, `Channel`, `Pornstar`, `Creator`, `Short`: Weitere API-Modelle. Sie sind fuer den einfachen Video-Download nicht erforderlich.

### eaf-base-api

- `Helper`: Gemeinsame API-Basisklasse fuer Iteratoren und Konstruktoren.
- `BaseCore`: Netzwerk-, Sitzungs-, Cache-, Playlist- und Download-Engine.
- `BaseCore.request(...)`: Fuehrt HTTP-Anfragen aus und behandelt Statuscodes, Wiederholungen und Netzfehler.
- `BaseCore.fetch_text(...)`: Laedt eine Antwort und dekodiert sie als Text.
- `BaseCore.fetch_bytes(...)`: Laedt binare Inhalte, vor allem HLS-Segmente.
- `BaseCore.download(configuration)`: Startet den HLS-Download anhand einer `DownloadConfigHLS`.
- `BaseCore.threaded_download(...)`: Laedt Segmente parallel, behandelt Statusdatei, Abbruch und Zusammenfuehrung.
- `DownloadConfigHLS`: Konfigurationsobjekt fuer Qualitaet, Ziel, Callback, Playlist, Remuxing und Fortsetzen.
- `AsyncSession`: Asynchroner HTTP-Client aus `curl_cffi`.
- `Cache`: Antwort- und Segment-Cache.
- `HTTPLogHandler`: Optionaler Handler fuer entfernte Logmeldungen.

## 4. Datenfluss

```text
main()
  |
  +--> Client()
  |      |
  |      +--> BaseCore.initialize_session()
  |
  +--> Client.get_video(url)
  |      |
  |      +--> Video(url, core)
  |      +--> Video.init()
  |             |
  |             +--> BaseCore.fetch_text(url)
  |                    |
  |                    +--> BaseCore.request(url)
  |
  +--> Video.download(...)
         |
         +--> Video.m3u8_base_url
         +--> DownloadConfigHLS(...)
         +--> BaseCore.download(configuration)
                |
                +--> fetch Playlist
                +--> get_segments(...)
                +--> fetch_bytes(segment) parallel
                +--> state file / segment files
                +--> optional remux
                +--> MP4-Zieldatei
```

## 5. Moegliche Bottlenecks

### Netzwerk

- Viele HLS-Segmente erzeugen viele HTTP-Anfragen.
- Die maximale Parallelitaet ist durch `max_workers_download` begrenzt, standardmaessig 20.
- Zu hohe Parallelitaet kann Serverlimits, lokale Bandbreitenlimits oder Rate Limits ausloesen.
- Die konfigurierte Timeout-Zeit betraegt standardmaessig 20 Sekunden.
- Wiederholungen mit Wartezeit verlaengern den Download bei instabilen Antworten.
- DNS, TLS, HTTP/2, Proxy oder Browser-Impersonation koennen vor dem eigentlichen Download bremsen.

### Server und Seitenschutz

- Die Website kann Challenge-Seiten, Bot-Schutz oder geaenderte HTML-Strukturen liefern.
- Die M3U8-URL kann ablaufen oder nur kurz gueltig sein.
- Die Segmente koennen andere Cookies, Header oder eine gueltige Referer-Sitzung verlangen.
- Ein Server kann viele parallele Anfragen absichtlich verlangsamen oder blockieren.

### Lokales System

- Schreiben vieler Segmentdateien kann durch Virenscanner oder langsame Datentraeger gebremst werden.
- Zu wenig freier Speicher kann das Zusammenfuehren oder Remuxing stoppen.
- Remuxing benoetigt zusaetzliche CPU, RAM, temporaeren Speicher und eine funktionierende PyAV-Installation.
- Die Ausgabe auf der Konsole kann bei sehr vielen Callback-Aufrufen selbst zum kleinen Engpass werden.
- Der Prozess benoetigt ausreichend Dateihandles fuer parallele Segmentzugriffe.

### Cache und Fortsetzen

- Eine defekte oder unvollstaendige Statusdatei kann einen Resume-Versuch stoeren.
- Ein alter Segmentordner kann nicht mehr zur aktuellen Playlist passen.
- Ein abgelaufener Cacheeintrag kann eine erneute Netzwerkanfrage erzwingen.

## 6. 403, Firewall und andere Netzwerkblockaden

Ja, ein HTTP-403 ist moeglich. In `BaseCore.request(...)` werden HTTP 401 und 403 als `AccessDeniedError("Request blocked by server!")` behandelt. Das bedeutet: Der Server hat die Anfrage empfangen und aktiv abgelehnt.

Typische Ursachen fuer 403:

- Bot-Schutz oder WAF der Website.
- Fehlende oder abgelaufene Cookies.
- Fehlende Browser-Header oder unpassende Browser-Impersonation.
- Zu viele Anfragen in kurzer Zeit.
- IP-Sperre oder Geoblocking.
- Ein abgelaufener signierter M3U8- oder Segment-Link.
- Ein Proxy, dessen IP vom Zielserver blockiert wird.

Eine lokale Windows-Firewall erzeugt normalerweise keinen HTTP-403. Sie blockiert eher die Verbindung. Typische lokale Symptome sind:

- Timeout.
- Verbindungsabbruch.
- TLS- oder Zertifikatsfehler.
- DNS-Fehler.
- Proxy- oder Socket-Fehler.

Ein 403 kommt dagegen normalerweise vom Server oder von einem vorgeschalteten Server. Die Firewall kann trotzdem indirekt beteiligt sein, wenn sie Python, `curl_cffi`, DNS, einen Proxy oder ausgehende HTTPS-Verbindungen blockiert. Dann sollte im Log eher ein Timeout, `RequestsError` oder ein TLS-Fehler auftauchen.

## 7. Alle relevanten geloggten Fehler und Warnungen

### Anwendungsebene

`downoader.py` protokolliert:

- `Download wird gestartet.`
- verwendete URL.
- absoluten Zielordner.
- `Verbindung wird hergestellt ...`
- `Videodaten werden abgerufen ...`
- `Videodaten erfolgreich abgerufen.`
- `Download laeuft ...`
- Fortschritt in Bytes und Prozent.
- `Download erfolgreich beendet: ...`
- `FEHLER: <ExceptionTyp>: <Meldung>`.
- Vollstaendigen Traceback ueber `traceback.print_exc()`.

### Netzwerk- und HTTP-Logging

`BaseCore` loggt unter anderem:

- erfolgreich abgerufene URL.
- aus dem Cache gelieferte Inhalte.
- erkannte Challenge-Seite.
- eine bereits durch einen anderen Task geloeste Challenge.
- erfolgreiche Challenge-Aufloesung.
- Sicherheitsabbruch wegen unerlaubter Zeichen im Challenge-Code.
- fehlgeschlagene Challenge-RegEx.
- Rate Limit 429 und vom Server geforderte Pause.
- Serverfehler 5xx mit erneutem Versuch.
- sonstige HTTP-Statuscodes.
- Request-Fehler inklusive Traceback.
- Timeout- oder Read-Fehler inklusive Traceback.
- unerwartete Netzwerkfehler inklusive Traceback.
- aufgebrauchtes Request-Wiederholungslimit.
- nicht dekodierbare Antwort mit Ausweichdekodierung Latin-1.

### Playlist- und Segment-Logging

- M3U8-Playlist wird angefordert.
- Variante statt Media-Playlist wird erkannt und auf eine Unterplaylist aufgeloest.
- Initialisierungssegment wird gefunden oder fehlt.
- Anzahl erkannter Segmente.
- Segmente werden im Cache gespeichert.
- ein Segmentdownload ist fehlgeschlagen und wird spaeter erneut versucht.
- vorhandene Statusdatei wird fuer Resume verwendet.
- keine Statusdatei vorhanden; Download startet neu.
- Statusdatei konnte nicht geladen werden; Download startet neu.
- Segmentverzeichnis wird aus der Statusdatei gesetzt.
- Segmentplan und Zielanzahl werden ausgegeben.
- bereits heruntergeladene Segmente werden ausgegeben.
- Zielsegmente werden ausgegeben.
- Abbruch wurde bereits vor dem Planen erkannt.
- laufende HLS-Anfragen werden abgebrochen.
- Segmentdateien werden im Speicher oder auf der Festplatte zusammengefuehrt.
- Statusdatei wird geschrieben.

### Remux- und Dateisystem-Logging

- Remuxing wird gestartet.
- Groesse der Eingabedatei wird ermittelt oder ist nicht verfuegbar.
- PyAV-Import fuer Remuxing ist fehlgeschlagen.
- Eingabedatei wird geoeffnet.
- erkanntes Eingabeformat.
- Audio ist MP4-kompatibel und wird ohne Transcoding uebernommen.
- Audio wird nach AAC transkodiert.
- `os.replace` ist fehlgeschlagen; manueller Kopiervorgang wird versucht.
- Remuxing wurde erfolgreich beendet.
- Download wurde erfolgreich abgeschlossen.
- unbehandelte Ausnahme im Download-Wrapper.

## 8. Alle relevanten Throws und Fehlerklassen

### Fehler bei Requests

- `AccessDeniedError`: HTTP 401 oder 403.
- `ResourceGone`: HTTP 410, Ressource existiert nicht mehr.
- `RateLimitError`: HTTP 429, optional mit `Retry-After`.
- `HTTPStatusError`: sonstige HTTP-Fehler sowie 5xx.
- `RequestRetriesExhausted`: alle erlaubten Request-Versuche sind fehlgeschlagen.
- `NetworkRequestError`: erneuter Versuch, zum Beispiel nach Challenge-Aufloesung.
- `ProxySSLError`: Proxy-Zertifikat oder TLS-Verifikation fehlgeschlagen.
- `InvalidProxy`: Proxy-Konfiguration oder Proxy-Verbindung ungueltig.
- `UnknownError`: nicht klassifizierter Netzwerk- oder Cookiefehler.
- `RequestsError`: Ursprungstyp von `curl_cffi` fuer Request-Probleme.

### Bot-Schutz und Sicherheit

- `BotProtectionDetected`: Bot-Schutz wurde erkannt.
- `BotDetection`: xhamster_api-spezifische Umwandlung des Bot-Schutz-Fehlers.
- `ChallengeRegexError`: Challenge-Seite erkannt, aber Challenge-Daten nicht extrahiert.
- `ChallengeMathError`: Challenge-Berechnung ist fehlgeschlagen.
- `SecurityAbort`: Der Challenge-Code enthaelt unerlaubte Zeichen.

Der Sicherheitsabbruch ist beabsichtigt. Der Code aus einer entfernten Antwort wird nur nach einer Zeichenpruefung ausgefuehrt. Bei einem solchen Fehler sollte die Anfrage nicht durch weitere Workarounds umgangen werden.

### Playlist und Download

- `PlaylistExtractionError`: Playlist konnte nicht gelesen oder aufgeloest werden.
- `ModuleNotFoundError`: erforderliches Modul wie `m3u8` oder PyAV fehlt.
- `TypeError`: falsche Download-Konfiguration oder inkompatibler API-Aufruf.
- `UnknownError("No segments found for this playlist.")`: Playlist enthaelt keine verwertbaren Segmente.
- `UnknownError("Segment state is invalid or empty.")`: Statusdatei ist ungueltig.
- `UnknownError("Segment state is missing segment_dir.")`: Statusdatei enthaelt keinen Segmentordner.
- `DownloadCancelled`: Download wurde ueber das Abbruchsignal beendet.
- `SegmentError`: Segment konnte nicht verarbeitet werden.
- `StateLoadError`: Statusdatei konnte nicht verarbeitet werden.
- `MaxRetriesExceeded`: maximale Wiederholungszahl fuer einen Downloadschritt wurde ueberschritten.

### Remuxing

- `ModuleNotFoundError`: PyAV ist fuer `remux=True` nicht installiert.
- `ValueError`: ungueltiges Audio-, Format- oder Konfigurationsargument.
- Datei- und Betriebssystemfehler beim Lesen, Schreiben, Verschieben oder Loeschen temporaerer Dateien.
- Ausnahme beim Audio-Transcoding.
- Ausnahme beim Ersetzen oder manuellen Kopieren der Ergebnisdatei.

### Downloader selbst

`downoader.py` faengt jedes `Exception` ab, gibt Typ und Meldung aus und druckt den Traceback. Danach wird der Fehler mit `raise` erneut weitergegeben. Dadurch bleibt der Prozessfehler fuer PowerShell sichtbar und der Exit-Code bleibt ungleich null.

## 9. Auffaellige Kompatibilitaetsrisiken

Der groesste aktuelle Risikofaktor ist nicht die URL, sondern der Versionsmix. `xhamster_api 2.2` verwendet noch alte Konstruktor- und Iterator-Annahmen, waehrend `eaf-base-api 4.1.1` bereits die neuen Konfigurationsobjekte verwendet.

Besonders auffaellig:

- `Something` ruft noch `alternative_constructor` auf.
- `Something.videos()` verwendet noch die alte `Helper.iterator(...)`-Signatur.
- Der einfache `Client.get_video(...)`- und `Video.download(...)`-Pfad ist angepasst.
- Andere API-Funktionen wie Channel-, Creator-, Short- oder Suchfunktionen koennen weitere Kompatibilitaetsfehler zeigen.

## 10. Empfehlungen

1. Die aktuell funktionierenden Paketversionen in einer `requirements.txt` oder virtuellen Umgebung festhalten.
2. Die lokalen Aenderungen unter `site-packages` als Patch oder eigene kompatible Adapterdatei sichern.
3. Bei 403 zuerst Serverblockade, Cookies, Rate Limit und Proxy pruefen; nicht automatisch die Firewall als Ursache annehmen.
4. Bei Timeout/TLS/Proxy-Fehlern Windows-Firewall, Virenscanner, Proxy und `verify_ssl` untersuchen.
5. Fuer lange Downloads `segment_state_path` beibehalten.
6. Bei wiederholten 403 oder 429 die Parallelitaet und Anfragefrequenz reduzieren.
7. Vor `remux=True` die PyAV-Installation und den freien Speicher pruefen.
8. Einen Testlauf mit einer einzelnen Playlist oder wenigen Segmenten vorsehen, bevor grosse Downloads gestartet werden.
