# Architecture guide

As of 2026-09-13.

## What the application is

An asynchronous desktop downloader for video. It takes a URL, resolves it to
provider-neutral metadata, picks what to download, fetches it, and leaves one
playable file behind. Three shapes of source are handled: segmented HLS
playlists, single progressive files, and separate video and audio tracks that
have to be muxed back together.

The application code lives under `src/video_downloader`. The HTTP, HLS and
remux machinery comes from `eaf_base_api`, and one site adapter from
`xhamster_api`; both are pinned Git dependencies, not vendored copies.

## Components

```text
src/video_downloader/__main__.py  ·  debug_main.py
        |
        v
    bootstrap.py            logging, exception handlers, qasync QSelectorEventLoop
        |
        +--> MainWindow             URL entry, job list, folder and login menu
        |
        +--> DownloadManager        holds the jobs, semaphore of 3
               |
               +--> DownloadJob     one download's state; observable, loop-serialised
               |
               +--> run_download_job (application/download_service.py)
                      |
                      +--> ProviderSession        one per job, closed by that job
                      |      |
                      |      +--> ProviderRegistry.resolve(url) -> Media
                      |      |      +--> XHamsterAdapter     xhamster.com / .desi
                      |      |      +--> PeerTubeAdapter     any instance's watch URL
                      |      |      +--> YouTubeAdapter      via yt-dlp
                      |      |      +--> XAdapter            x.com / twitter.com posts
                      |      |      +--> DirectMediaAdapter  a bare .m3u8 and friends
                      |      |
                      |      +--> BaseCore        the provider-clean download engine
                      |
                      +--> track_selection    what to download, from the Media alone
                      +--> track_download     fetching it, one path per source type
                      +--> muxing             two tracks into one container, losslessly
```

### Entry points

`__main__.py` starts the production build, `debug_main.py` the debug one - the
latter with `faulthandler`, a UI-freeze watchdog and console logging. Both call
`bootstrap.run_application`. The console downloader is
`cli/console_app.py` and imports no Qt at all.

### Application layer

`DownloadManager` owns the jobs and limits concurrency with an
`asyncio.Semaphore(3)`. `run_download_job` drives one job through its states and
knows no website: it asks the registry for a `Media`, lets `track_selection`
choose, and hands the result to either the engine or the resolver.

Two operations that used to be one are now separate:

* `cancel_download` stops the run and leaves everything on disk, including the
  resume state.
* `delete_download(job, delete_file=...)` removes the job. The keyword has **no
  default**, so every caller states whether the file goes with it.

### The job as an observable value

`DownloadJob` is what the user interface reads. Three things about it matter
more than its fields:

* **It has listeners, not a listener.** `add_listener` / `remove_listener`;
  a listener that raises is logged and skipped rather than stopping the
  download.
* **It applies its own changes on the loop that owns it.** Progress arrives from
  worker threads - the yt-dlp hook, the mux, the engine's remux - and
  `bind_loop()` plus `call_soon_threadsafe` is what serialises them. A change
  from the loop thread queues too while anything is pending, so completion never
  overtakes the progress it was meant to follow. `seq` makes the order
  comparable.
* **It can describe itself without the things that run it.** `JobSnapshot.of(job)`
  is the same state minus the task, the event, the callables and the exception.

Failures are values, not sentences: `JobError(kind, code, message, retryable,
cause)`, classified in `application/job_failure.py` from the exception hierarchy
that already existed. `str(job.error)` still produces the text a user reads.

### UI layer

`MainWindow` (PySide6) shows the job list and asks the two questions the core
cannot answer itself - whether a large download should start, and whether the
user wants to sign in to a site. Both are awaited rather than called, so the
other downloads keep running while a dialog is open.

`SiteLoginWindow` shows a site's own login page in an off-the-record Qt WebEngine
profile and keeps nothing but the session cookies. It is imported lazily; a run
that never signs in never loads WebEngine.

### Provider layer

The registry selects exactly one provider by `supports(url)` and returns its
`resolve(url)` as a provider-neutral `Media` with a list of `MediaSource`. No
match is an `UnsupportedURLError`, more than one an `AmbiguousProviderError`.
Registration happens in `bootstrap.create_provider_session`, and nowhere else.

Two transport contexts are deliberately kept apart:

* **Extraction.** Each adapter owns the client it scrapes with. Whatever that
  session accumulates - a `Referer`, a cookie set during a page fetch - stays
  there and dies with `registry.close()`.
* **Media download.** What a media request must carry travels on
  `MediaSource.headers` and is applied per request by `BaseCore`, on the master
  manifest, the media playlist and every segment including retries. The download
  core stays provider-clean, so a direct `.m3u8` never inherits a neighbour's
  `Referer`.

### Where files go

Nothing is resolved against the working directory. `infrastructure/paths.py`
puts configuration, logs and the session store in a per-user location
(`%LOCALAPPDATA%\VideoDownloader` on Windows). The download directory itself is
a user decision, persisted in `settings.json`, and asked for when it is missing.

## Lifecycle

```text
CREATED -> QUEUED -> CONNECTING -> FETCHING_METADATA -> DOWNLOADING
                                                          |
                                                    (MUXING) -> COMPLETED
                                                          |
                                              CANCELLED / FAILED
```

`MUXING` exists so the bar does not sit at 100 % with no file to open and
nothing on screen explaining the wait.

## Maintenance rules

1. **Nothing blocking on the loop.** A `threading.Event().wait()` or a network
   call in the Qt loop freezes the window; use `asyncio.to_thread`.
2. **Provider isolation.** Every job gets its own `ProviderSession` from the
   injected factory, and `run_download_job` closes it in `finally` - on failure
   and on cancellation too. Media transport headers belong on
   `MediaSource.headers`, never on the download core's session.
3. **A new website** is registered in `bootstrap.create_provider_session` and
   changes nothing else. The download workflow knows `Media` and `MediaSource`,
   not websites.
4. **Cancellation** goes through `job.request_stop()`, which sets the stop event
   immediately rather than queueing it.
5. **The event loop kind is an application decision.** curl_cffi drives libcurl
   through `add_reader`/`add_writer`, which a Windows proactor loop does not
   have. The GUI builds `qasync.QSelectorEventLoop`; everything else goes
   through `infrastructure.event_loop.new_event_loop`.
