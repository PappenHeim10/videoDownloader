import asyncio
import faulthandler
import logging
import os
import platform
import sys
import threading
import time
from functools import partial
from pathlib import Path
from typing import Callable

from base_api import BaseCore, DirectMediaAdapter, ProviderRegistry
from base_api.modules.config import RuntimeConfig
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from xhamster_api import XHamsterAdapter

from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob
from video_downloader.infrastructure.paths import AppPaths
from video_downloader.infrastructure.settings import AppSettings
from video_downloader.providers import PeerTubeAdapter
from video_downloader.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

#: Emitted on stdout by `--smoke-test` only. Reaching this line means the frozen
#: application imported video_downloader, imported bootstrap, and constructed its
#: components - which is the thing a build needs proven.
SMOKE_MARKER = "VideoDownloader smoke OK"

#: Frameworks whose DEBUG output is about their own plumbing rather than about
#: this application. Named explicitly so raising our level never raises theirs.
THIRD_PARTY_LOG_LEVELS = {
    "qasync": logging.WARNING,
    "asyncio": logging.WARNING,
}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def create_provider_session() -> ProviderSession:
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
    """
    core = BaseCore(RuntimeConfig())
    registry = ProviderRegistry()
    registry.register(XHamsterAdapter())
    registry.register(PeerTubeAdapter())
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

def configure_logging(debug: bool, paths: AppPaths | None = None):
    handlers = []
    # Per-user location, never the working directory: the same executable started
    # from Explorer, a shortcut or a terminal must write to the same log.
    log_dir = (paths or AppPaths.default()).ensure_log_dir()

    if debug:
        file_handler = logging.FileHandler(log_dir / "downloader-debug.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler(sys.stdout)
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

def run_application(*, debug: bool = False, smoke_test: bool = False) -> int:
    loop_debug_enabled = debug and _env_flag("DOWNLOADER_ASYNCIO_DEBUG", default=False)
    
    configure_logging(debug)
    
    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception
    
    if debug:
        faulthandler.enable()
        import PySide6
        import qasync
        qasync_version = getattr(qasync, "__version__", "unknown")
        frozen = getattr(sys, "frozen", False)
        print("="*50)
        print("VideoDownloader DEBUG BUILD")
        print("="*50)
        print(f"Python: {platform.python_version()}")
        print(f"PySide6: {PySide6.__version__}")
        print(f"qasync: {qasync_version}")
        print(f"Platform: {platform.platform()}")
        print(f"Executable: {sys.executable} (Frozen: {frozen})")
        print(f"Working directory: {Path.cwd()}")
        print(f"asyncio debug: {'ON' if loop_debug_enabled else 'OFF'}")
        print(f"watchdog: ON")
        print("="*50)

    logger.info("Application starting (debug=%s, smoke_test=%s)", debug, smoke_test)
    application = QApplication.instance()
    if not application:
        application = QApplication(sys.argv)
    
    # The directory comes from the user's persisted choice, and may legitimately
    # be absent on first run - the window asks for one when a download starts.
    settings = AppSettings()
    manager = DownloadManager(
        output_dir=settings.get_download_directory(),
        max_concurrent_downloads=3,
        job_runner=create_job_runner(),
    )
    window = MainWindow(manager, settings)

    if smoke_test:
        logger.info("Smoke test passed: Components constructed successfully")
        # Printed only in smoke mode, never during a normal start. The build reads
        # it to prove it exercised *this* artifact: an exit code alone cannot tell
        # a working executable from a stale one lying in another directory.
        print(SMOKE_MARKER, flush=True)
        return 0
        
    loop = QEventLoop(application)
    asyncio.set_event_loop(loop)
    
    if loop_debug_enabled:
        loop.set_debug(True)
        loop.slow_callback_duration = 0.2
        
    loop.set_exception_handler(handle_asyncio_exception)
    
    logger.info("Qt event loop / qasync initialized")
    
    window.show()
    
    watchdog = None
    tick_timer = None
    if debug:
        watchdog = WatchdogThread(loop)
        watchdog.start()
        
        from PySide6.QtCore import QTimer
        tick_timer = QTimer(application)
        tick_timer.timeout.connect(watchdog.tick)
        tick_timer.start(200)
        
    logger.info("Starting loop.run_forever()")
    try:
        with loop:
            loop.run_forever()
    finally:
        logger.info("Event loop stopped")
        if watchdog:
            watchdog._running = False
            watchdog.join(2.0)
            
    pending = asyncio.all_tasks(loop)
    if pending:
        logger.warning(f"Pending tasks on exit: {len(pending)}")
        for t in pending:
            logger.debug(f"Pending task: {t.get_name()} {t.get_coro()}")
            
    logger.info("Application shutdown complete")
    return 0
