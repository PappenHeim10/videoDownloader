# The download flow

How one download actually runs, from the URL to the finished file.

## 1. The components involved

*   **`DownloadJob` (`domain/download_job.py`)**
    One download's complete state: URL, quality, title, `LifecycleState`,
    progress counters and an `asyncio.Event` for cancellation. It is also the
    thing the user interface watches - see §4.

*   **`JobSnapshot` (`domain/job_snapshot.py`)**
    The same state with nothing runnable in it: no task, no event, no callable,
    no exception. What a consumer reads when it should not hold the job itself.

*   **`JobError` (`domain/job_error.py`)**
    Why a job failed, as a value: category, code, message, whether a retry can
    change anything, and the original exception for the log. Classified in
    `application/job_failure.py` from the exception hierarchy that already
    existed; `str(error)` is still the sentence a user reads.

*   **`DownloadManager` (`application/download_manager.py`)**
    Holds the jobs, starts and stops them, and limits concurrency with an
    `asyncio.Semaphore(3)`. Cancelling and deleting are separate operations.

*   **`run_download_job` (`application/download_service.py`)**
    The asynchronous core of a download. It knows no website: it takes a
    `ProviderSession`, lets the registry resolve the URL to a `Media`, picks
    what to fetch, and fetches it.

*   **`ProviderSession` (`application/provider_session.py`)**
    One job's provider resources - the registry plus the `BaseCore` it downloads
    with. Adapters own their extraction clients; the download core stays
    provider-clean. Composed in `bootstrap.create_provider_session`, closed
    exactly once by the job that created it.

*   **Entry points**
    `__main__.py` starts the Qt interface on a qasync loop; `debug_main.py` does
    the same with debug logging and a watchdog; `cli/console_app.py` runs
    headless on a plain selector loop and imports no Qt.

## 2. The phases

1.  **Created (`CREATED` -> `QUEUED`)**
    `DownloadManager.add_download()` builds the job, binds it to the running
    loop, registers it, and starts an `asyncio.Task`.

2.  **Connecting (`CONNECTING`)**
    The injected factory builds this job's `ProviderSession`: its own registry
    with adapter-owned extraction clients, and its own provider-clean download
    core.

3.  **Metadata (`FETCHING_METADATA`)**
    `await session.registry.resolve(url)` selects exactly one provider and
    returns a `Media`. Each `MediaSource` carries its own transport headers.

    If no provider matches, or more than one does, the job fails here without
    downloading anything - logged apart from network and extraction failures, so
    "this link is not ours" never reads like a broken connection.

    If a provider refuses only because nobody is signed in, and somebody is
    there to ask, the login window opens and the resolution is retried exactly
    once.

    The output path is built from the title through `strip_title()`, with the
    extension of whatever was actually chosen - a file called `.mp4` that is
    WebM inside is a lie the user only finds in a player.

4.  **Selection and the size question**
    `track_selection` picks from the `Media` alone - no requests, no guessing.
    Either one finished file, or a video and an audio track to combine. If the
    estimate exceeds 2 GiB and there is somebody to ask, the question is awaited
    before the first byte; the other downloads keep running while it is open.

5.  **Downloading (`DOWNLOADING`)**
    Two paths, chosen per track by the source's own type:

    * **The engine's transports.** HLS fetches the segments concurrently;
      progressive HTTP owns resume, retries and atomic finalisation.
    * **The resolver.** For a source only yt-dlp can produce - unless one
      one-byte request at the far end of the file proves our own transport can
      read all of it, in which case it does.

    Progress is reported through a callback coalesced to at most one update
    every 0.15 s. Across several phases it is aggregated into one bar, and while
    any phase is still unweighed the total is reported as zero, which means
    "end unknown" rather than a percentage that would still move.

6.  **Muxing (`MUXING`)**
    Two tracks go into one container, packet for packet, never re-encoded. Its
    own state because otherwise the bar sits full with no file to open.

7.  **Finishing (`COMPLETED` / `CANCELLED` / `FAILED`)**
    A stop request ends the job as cancelled. A failure records a `JobError` and
    the reason. Success sets the progress to 100 % and fills in the total from
    what actually arrived, so a download nobody could weigh still reports that
    it finished. The resume state is removed, and the `ProviderSession` is
    closed in `finally` - once, on every path.

## 3. Resume

Each track writes its progress to its own state file while it downloads. A
cancelled two-track job keeps the track that finished, so resuming costs one
download rather than two.

## 4. How the interface finds out

`DownloadJob` notifies its listeners on every change - registered with
`add_listener`, removed with `remove_listener`. A listener that raises is logged
and skipped; a fault in a user interface must not stop a download.

Progress arrives from worker threads: the yt-dlp hook, the mux and the engine's
own remux all run in `asyncio.to_thread`. The job serialises those onto the loop
it was bound to, and while anything is pending a change from the loop thread
queues behind it - otherwise completion would overtake the last progress it was
meant to follow. `seq` increments on every change, so two observations of the
same job are comparable.
