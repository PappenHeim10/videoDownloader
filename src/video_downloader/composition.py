"""Everything the application needs to start that has nothing to do with a window.

Split out of `bootstrap` so that a caller without a user interface can reach
it: the console downloader, the test suite, and the host that serves a
separate front end. `bootstrap` imports Qt at module level, so anything that
went through it paid for a GUI toolkit whether or not it opened a window -
the CLI has been doing exactly that, deferring the import into a function
rather than avoiding it.

Nothing here may import Qt. `tests/host/test_no_qt_in_the_host_path.py`
enforces that from the outside, because an import added by accident is
invisible until something that cannot afford it pays for it.
"""

import faulthandler
import logging
import os
import sys
import threading
import time
from functools import partial
from typing import Any, Callable

from base_api import BaseCore, DirectMediaAdapter, ProviderRegistry
from base_api.modules.config import RuntimeConfig
from xhamster_api import XHamsterAdapter

from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob
from video_downloader.infrastructure.paths import AppPaths
from video_downloader.providers import PeerTubeAdapter, XAdapter, YouTubeAdapter
from video_downloader.providers.x import XLoginRequiredError

logger = logging.getLogger(__name__)

#: Emitted on stdout by `--smoke-test` only. Reaching this line means the frozen
#: application imported video_downloader, imported bootstrap, and constructed its
#: components - which is the thing a build needs proven.
SMOKE_MARKER = "VideoDownloader smoke OK"

#: Printed by `--smoke-test` once every yt-dlp extractor this application needs
#: has been reached. Separate from the marker above because it proves a
#: different thing, and because only one of the two can fail in a frozen build.
EXTRACTOR_MARKER = "VideoDownloader extractors OK"

#: The extractors this application resolves through, each with a URL it must
#: claim. yt-dlp names them itself; the URLs are the cheapest possible proof
#: that the named extractor is the one that answers.
REQUIRED_EXTRACTORS = {
    "Youtube": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
    "Twitter": "https://x.com/example/status/1234567890123456789",
}

#: Frameworks whose DEBUG output is about their own plumbing rather than about
#: this application. Named explicitly so raising our level never raises theirs.
THIRD_PARTY_LOG_LEVELS = {
    "qasync": logging.WARNING,
    "asyncio": logging.WARNING,
}


def verify_extractors() -> None:
    """Prove the artifact can reach the extractors it needs, not merely import them.

    yt-dlp resolves extractors by name at run time, so nothing in the import
    graph points at them. A frozen build without them imports `yt_dlp` happily
    and then reports every YouTube or X URL as unsupported - a failure that
    exists only in the artifact and never in a source run, which is exactly the
    kind the smoke test is for.

    Asked through yt-dlp's own registry rather than by importing a module path,
    because the path has moved between releases and the registry has not.
    """
    from yt_dlp.extractor import get_info_extractor

    for name, url in REQUIRED_EXTRACTORS.items():
        extractor = get_info_extractor(name)
        if not extractor.suitable(url):
            raise RuntimeError(
                f"The bundled {name} extractor does not claim its own URL form"
            )


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_provider_session(session_store: Any = None) -> ProviderSession:
    """Build the production registry, scoped to one job.

    This is the only place that knows which websites the application supports.
    Adding one means registering it here; the download workflow does not change.

    Why per job and not one shared registry for the whole application:
    `XHamsterAdapter` owns a `Client` -> `BaseCore` -> `curl_cffi.AsyncSession`,
    and closing an adapter is exactly what `ProviderRegistry.close()` does. A
    single shared registry would leave two bad options - close it after every
    job, which pulls the session out from under every other running job, or
    never close it per job, which makes concurrent downloads share one session
    where today each job has its own. Per-job isolation is the stronger
    guarantee, so it wins.

    Two transport contexts, kept deliberately apart:

    * Extraction: each adapter owns the client it scrapes with. Whatever that
      session accumulates - the xHamster `Referer` the `Client` installs, the
      cookies the site sets during the page fetch - stays confined to it and
      dies with `registry.close()`.
    * Download: `session.core` is the engine the job downloads on, and it is
      provider-clean by construction - no adapter ever touches its session.
      What a media request must carry travels on `MediaSource.headers` and is
      applied per request by `BaseCore`, so an xHamster source brings its
      `Referer` along and a direct `.m3u8` source stays header-free instead of
      inheriting one from a neighbour.

    The registration order is site adapters first, the direct-URL adapter last,
    and it carries no meaning beyond reading order: selection is by `supports()`
    alone, and two adapters claiming one URL stays an `AmbiguousProviderError`
    rather than being silently settled by position.

    `session_store` is where a site login lives, and it is passed in rather than
    built here for two reasons. The window that establishes a session and the
    resolver that uses it have to share one instance, or a job retrying after a
    login would read a cache written before it. And a default store would make
    every caller that builds a registry - the test suite included - read the
    developer's real per-user file. Without one, resolution is anonymous, which
    is what it was before there was a login at all.
    """
    core = BaseCore(RuntimeConfig())
    registry = ProviderRegistry()
    registry.register(XHamsterAdapter())
    registry.register(PeerTubeAdapter())
    registry.register(YouTubeAdapter())
    # The narrowest contract that works: the adapter is handed a way to ask for
    # its own site's session, not the store. It cannot read another site's, and
    # it asks per resolution, so a login is in effect the moment it finishes.
    registry.register(XAdapter(
        session_source=(
            partial(session_store.load, XLoginRequiredError.site)
            if session_store is not None else None
        ),
        # And the right to drop it, for the one case where keeping it is worse
        # than having none: a session X rejects makes even a public post fail.
        forget_session=(
            partial(session_store.clear, XLoginRequiredError.site)
            if session_store is not None else None
        ),
    ))
    registry.register(DirectMediaAdapter())
    return ProviderSession(registry=registry, core=core)


def create_job_runner(
    session_factory: Callable[[], ProviderSession] = create_provider_session,
) -> Callable[[DownloadJob], object]:
    """Bind the providers into the job runner.

    The manager stays provider-neutral: it is handed something it can call with
    a job, and never learns that providers exist.
    """
    return partial(run_download_job, session_factory=session_factory)


class WatchdogThread(threading.Thread):
    def __init__(self, loop, timeout=2.0):
        super().__init__(daemon=True, name="Watchdog")
        self.loop = loop
        self.timeout = timeout
        self._last_tick = time.monotonic()
        self._running = True

    def run(self):
        while self._running:
            now = time.monotonic()
            if now - self._last_tick > self.timeout:
                print(f"\n================ UI FREEZE DETECTED ({now - self._last_tick:.1f}s) ================", file=sys.stderr)
                faulthandler.dump_traceback()
                print("====================================================\n", file=sys.stderr)
                self._last_tick = now 
            time.sleep(0.5)

    def tick(self):
        self._last_tick = time.monotonic()

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

def handle_thread_exception(args):
    logger.critical("Uncaught thread exception in %s", args.thread.name if args.thread else "Unknown", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

def handle_asyncio_exception(loop, context):
    msg = context.get("exception", context["message"])
    logger.critical(f"Uncaught asyncio exception: {msg}", exc_info=context.get("exception"))

def configure_logging(debug: bool, paths: AppPaths | None = None, stream=None):
    """Set up logging. `stream` is where the debug console handler writes.

    It defaults to stdout, which is what the window and the CLI have always
    used. The host passes stderr, because stdout there carries the protocol
    handshake and a log line in that stream would read as a malformed message.
    """
    handlers = []
    # Per-user location, never the working directory: the same executable started
    # from Explorer, a shortcut or a terminal must write to the same log.
    log_dir = (paths or AppPaths.default()).ensure_log_dir()

    if debug:
        file_handler = logging.FileHandler(log_dir / "downloader-debug.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler(stream or sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        handlers.extend([file_handler, console_handler])
        level = logging.DEBUG
    else:
        file_handler = logging.FileHandler(log_dir / "downloader.log", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        handlers.append(file_handler)
        level = logging.INFO
        
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    for handler in handlers:
        handler.setFormatter(formatter)
        
    logging.basicConfig(level=level, handlers=handlers, force=True)

    # Turning on application DEBUG must not turn on third-party executor internals.
    # qasync logs every callback it dispatches together with its arguments, and the
    # HLS writer is dispatched as write_part(path, data) - so `data` is a whole
    # segment, and a debug download wrote megabytes of raw bytes into the log and
    # slowed visibly while doing it. Application loggers keep their level.
    for name, third_party_level in THIRD_PARTY_LOG_LEVELS.items():
        logging.getLogger(name).setLevel(third_party_level)

