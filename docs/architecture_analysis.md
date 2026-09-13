# Architecture analysis of the video downloader

As of 2026-08-27.

> **Historical snapshot.** This analysis describes the state before the
> repository was restructured: a single `downoader.py` script, packages patched
> in place under `site-packages`, and `xhamster_api 2.2` with
> `eaf-base-api 4.1.1`. None of those exist any more - there is no `downoader.py`
> and no `packages/` directory, the dependencies are pinned Git forks, and the
> application is a package under `src/video_downloader`. Kept because sections
> 5 to 8 still describe the engine's failure modes usefully. For the current
> architecture see `architecture.md` and `download_flow.md`.

## 1. Overview

`downoader.py` is an asynchronous HLS video downloader. The script uses
`xhamster_api` as a website adapter and `eaf-base-api` as its network, cache and
download engine.

The installed versions matter:

- `xhamster_api 2.2`
- `eaf-base-api 4.1.1`
- Python 3.13

These versions partly use different API generations, so compatibility
adjustments were needed in the installed package:

- `video_constructor` is accepted in `Helper` as an alias for `constructor`.
- `core.fetch(...)` was replaced by `core.fetch_text(...)`.
- `Video.download(...)` now builds a `DownloadConfigHLS` object and hands it to
  `BaseCore.download(...)`.
- `setup_logger(...)` was defined as an adapter onto `configure_app_logging(...)`.

The adjustments currently live under `Python313\Lib\site-packages`. Reinstalling
the packages can overwrite them.

## 2. What the downloader does

The sequence in `downoader.py`:

1. `asyncio.run(main())` starts the asynchronous event loop.
2. The video URL is set.
3. The target directory `Videos\downloads` is created.
4. A `Client` is created.
5. The client fetches the HTML page with `BaseCore.fetch_text(url)`.
6. `Video.init()` stores the HTML content in the `Video` object.
7. Properties such as the title and the M3U8 URL are extracted from the HTML on
   demand.
8. `Video.download(...)` creates a `DownloadConfigHLS`.
9. `BaseCore.download(...)` reads the M3U8 playlist.
10. The playlist is resolved into individual HLS segments.
11. The segments are downloaded concurrently.
12. The segments are joined in memory or in the segment directory.
13. With `remux=True` the transport stream data is converted into an MP4 file
    using PyAV/FFmpeg-compatible logic.
14. The state file `downloads\xhamster.state.json` allows an interrupted
    download to be resumed.
15. The callback prints progress to the console.

The script does not download a file from a single direct MP4 link. It downloads
an HLS playlist and many smaller segments.

## 3. Classes and functions involved

### Application

- `main()` in `downoader.py`: orchestrates the whole sequence.
- `custom_callback(downloaded, total)`: prints progress.
- `Path`: creates and manages the absolute target directory.
- `threading.Event`: intended as the cancellation signal.
- `traceback.print_exc()`: prints the full traceback on an error.

### xhamster_api

- `Client`: public API for `get_video(...)`.
- `Video`: holds the URL and HTML data and extracts the title and M3U8 URL.
- `Video.init()`: loads the video page.
- `Video.download(...)`: translates the old public download signature into
  `DownloadConfigHLS`.
- `get_html_content(...)`: loads HTML through `fetch_text(...)` and converts
  certain errors.
- `setup_logger(...)`: adapter onto the base package's logger function.
- `Something`, `Channel`, `Pornstar`, `Creator`, `Short`: further API models. Not
  required for a simple video download.

### eaf-base-api

- `Helper`: shared API base class for iterators and constructors.
- `BaseCore`: network, session, cache, playlist and download engine.
- `BaseCore.request(...)`: performs HTTP requests and handles status codes,
  retries and network errors.
- `BaseCore.fetch_text(...)`: loads a response and decodes it as text.
- `BaseCore.fetch_bytes(...)`: loads binary content, mainly HLS segments.
- `BaseCore.download(configuration)`: starts the HLS download from a
  `DownloadConfigHLS`.
- `BaseCore.threaded_download(...)`: downloads segments concurrently and handles
  the state file, cancellation and joining.
- `DownloadConfigHLS`: configuration object for quality, target, callback,
  playlist, remuxing and resuming.
- `AsyncSession`: asynchronous HTTP client from `curl_cffi`.
- `Cache`: response and segment cache.
- `HTTPLogHandler`: optional handler for remote log messages.

## 4. Data flow

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
                +--> fetch playlist
                +--> get_segments(...)
                +--> fetch_bytes(segment) concurrently
                +--> state file / segment files
                +--> optional remux
                +--> MP4 target file
```

## 5. Possible bottlenecks

### Network

- Many HLS segments produce many HTTP requests.
- Maximum concurrency is bounded by `max_workers_download`, 20 by default.
- Too much concurrency can trigger server limits, local bandwidth limits or rate
  limits.
- The configured timeout is 20 seconds by default.
- Retries with a wait extend the download when responses are unstable.
- DNS, TLS, HTTP/2, a proxy or browser impersonation can slow things down before
  the download proper.

### Server and site protection

- The website can serve challenge pages, bot protection or changed HTML
  structures.
- The M3U8 URL can expire or be valid only briefly.
- The segments can require different cookies, headers or a valid Referer
  session.
- A server can deliberately slow down or block many concurrent requests.

### Local system

- Writing many segment files can be slowed by virus scanners or slow storage.
- Too little free space can stop the joining or the remux.
- Remuxing needs additional CPU, RAM, temporary storage and a working PyAV
  installation.
- Console output can itself become a small bottleneck with very frequent
  callback invocations.
- The process needs enough file handles for concurrent segment access.

### Cache and resuming

- A broken or incomplete state file can disturb a resume attempt.
- An old segment directory may no longer match the current playlist.
- An expired cache entry can force another network request.

## 6. 403, firewall and other network blocks

Yes, an HTTP 403 is possible. In `BaseCore.request(...)` HTTP 401 and 403 are
turned into `AccessDeniedError("Request blocked by server!")`. That means the
server received the request and actively refused it.

Typical causes of a 403:

- Bot protection or the site's WAF.
- Missing or expired cookies.
- Missing browser headers or unsuitable browser impersonation.
- Too many requests in a short time.
- An IP ban or geoblocking.
- An expired signed M3U8 or segment link.
- A proxy whose IP the target server blocks.

A local Windows firewall does not normally produce an HTTP 403. It blocks the
connection instead. Typical local symptoms are:

- Timeout.
- Connection dropped.
- TLS or certificate errors.
- DNS errors.
- Proxy or socket errors.

A 403, by contrast, normally comes from the server or from a server in front of
it. The firewall can still be indirectly involved if it blocks Python,
`curl_cffi`, DNS, a proxy or outbound HTTPS. In that case the log should show a
timeout, a `RequestsError` or a TLS error rather than a 403.

## 7. Relevant logged errors and warnings

### Application level

`downoader.py` logs:

- `Download wird gestartet.`
- the URL used.
- the absolute target directory.
- `Verbindung wird hergestellt ...`
- `Videodaten werden abgerufen ...`
- `Videodaten erfolgreich abgerufen.`
- `Download laeuft ...`
- progress in bytes and percent.
- `Download erfolgreich beendet: ...`
- `FEHLER: <ExceptionType>: <message>`.
- the full traceback through `traceback.print_exc()`.

### Network and HTTP logging

`BaseCore` logs, among other things:

- the URL fetched successfully.
- content served from the cache.
- a detected challenge page.
- a challenge already solved by another task.
- a successful challenge resolution.
- a security abort because of disallowed characters in the challenge code.
- a failed challenge regex.
- rate limit 429 and the pause the server asked for.
- server errors 5xx with a retry.
- other HTTP status codes.
- request errors including the traceback.
- timeout or read errors including the traceback.
- unexpected network errors including the traceback.
- the request retry limit being used up.
- an undecodable response with the Latin-1 fallback decoding.

### Playlist and segment logging

- the M3U8 playlist being requested.
- a variant rather than a media playlist being detected and resolved to a
  sub-playlist.
- the initialisation segment being found or missing.
- the number of segments detected.
- segments being stored in the cache.
- a segment download having failed and being retried later.
- an existing state file being used for a resume.
- no state file present; the download starts fresh.
- the state file could not be loaded; the download starts fresh.
- the segment directory being set from the state file.
- the segment plan and target count.
- the segments already downloaded.
- the target segments.
- cancellation already detected before planning.
- running HLS requests being cancelled.
- segment files being joined in memory or on disk.
- the state file being written.

### Remux and filesystem logging

- remuxing starting.
- the input file's size being determined, or unavailable.
- the PyAV import for remuxing having failed.
- the input file being opened.
- the detected input format.
- audio being MP4-compatible and carried over without transcoding.
- audio being transcoded to AAC.
- `os.replace` having failed; a manual copy being attempted.
- remuxing having finished successfully.
- the download having completed successfully.
- an unhandled exception in the download wrapper.

## 8. Relevant throws and error classes

### Request errors

- `AccessDeniedError`: HTTP 401 or 403.
- `ResourceGone`: HTTP 410, the resource no longer exists.
- `RateLimitError`: HTTP 429, optionally with `Retry-After`.
- `HTTPStatusError`: other HTTP errors as well as 5xx.
- `RequestRetriesExhausted`: every permitted request attempt failed.
- `NetworkRequestError`: another attempt, for example after a challenge was
  resolved.
- `ProxySSLError`: proxy certificate or TLS verification failed.
- `InvalidProxy`: proxy configuration or proxy connection invalid.
- `UnknownError`: unclassified network or cookie error.
- `RequestsError`: the originating type from `curl_cffi` for request problems.

### Bot protection and security

- `BotProtectionDetected`: bot protection was detected.
- `BotDetection`: xhamster_api's own conversion of the bot protection error.
- `ChallengeRegexError`: a challenge page was detected but the challenge data
  could not be extracted.
- `ChallengeMathError`: the challenge computation failed.
- `SecurityAbort`: the challenge code contains disallowed characters.

The security abort is deliberate. Code from a remote response is executed only
after a character check. On such an error the request should not be worked
around further.

### Playlist and download

- `PlaylistExtractionError`: the playlist could not be read or resolved.
- `ModuleNotFoundError`: a required module such as `m3u8` or PyAV is missing.
- `TypeError`: wrong download configuration or an incompatible API call.
- `UnknownError("No segments found for this playlist.")`: the playlist contains
  no usable segments.
- `UnknownError("Segment state is invalid or empty.")`: the state file is
  invalid.
- `UnknownError("Segment state is missing segment_dir.")`: the state file names
  no segment directory.
- `DownloadCancelled`: the download was ended through the cancellation signal.
- `SegmentError`: a segment could not be processed.
- `StateLoadError`: the state file could not be processed.
- `MaxRetriesExceeded`: the maximum retry count for a download step was
  exceeded.

### Remuxing

- `ModuleNotFoundError`: PyAV is not installed for `remux=True`.
- `ValueError`: invalid audio, format or configuration argument.
- File and operating system errors while reading, writing, moving or deleting
  temporary files.
- An exception during audio transcoding.
- An exception while replacing or manually copying the result file.

### The downloader itself

`downoader.py` catches every `Exception`, prints the type and message, and
prints the traceback. The error is then re-raised, so the process failure stays
visible to PowerShell and the exit code stays non-zero.

## 9. Notable compatibility risks

The biggest current risk factor is not the URL but the version mix.
`xhamster_api 2.2` still uses the old constructor and iterator assumptions,
while `eaf-base-api 4.1.1` already uses the new configuration objects.

Particularly notable:

- `Something` still calls `alternative_constructor`.
- `Something.videos()` still uses the old `Helper.iterator(...)` signature.
- The simple `Client.get_video(...)` and `Video.download(...)` path has been
  adjusted.
- Other API functions such as channel, creator, short or search functions may
  show further compatibility errors.

## 10. Recommendations

1. Pin the package versions that currently work in a `requirements.txt` or a
   virtual environment.
2. Keep the local changes under `site-packages` as a patch or as a separate
   compatible adapter file.
3. On a 403, first check server blocking, cookies, rate limits and the proxy; do
   not assume the firewall automatically.
4. On timeout, TLS or proxy errors, investigate the Windows firewall, the virus
   scanner, the proxy and `verify_ssl`.
5. Keep `segment_state_path` for long downloads.
6. On repeated 403 or 429, reduce concurrency and request frequency.
7. Before `remux=True`, check the PyAV installation and the free space.
8. Plan a test run with a single playlist or a few segments before starting
   large downloads.
