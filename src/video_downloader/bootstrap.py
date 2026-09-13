"""Starting the application behind its Qt window.

The parts that do not need a window live in `composition`; this module is what
is left once they are taken out - the Qt application object, the Qt-driven
event loop, and the window itself.

The names below are re-exported because callers and tests have always imported
them from here.
"""

import asyncio
import faulthandler
import logging
import platform
import sys
import threading
from functools import partial
from pathlib import Path

from PySide6.QtWidgets import QApplication
from qasync import QSelectorEventLoop

from video_downloader.application.download_manager import DownloadManager
from video_downloader.composition import (  # noqa: F401 - re-exported
    EXTRACTOR_MARKER,
    REQUIRED_EXTRACTORS,
    SMOKE_MARKER,
    THIRD_PARTY_LOG_LEVELS,
    WatchdogThread,
    _env_flag,
    configure_logging,
    create_job_runner,
    create_provider_session,
    handle_asyncio_exception,
    handle_exception,
    handle_thread_exception,
    verify_extractors,
)
from video_downloader.infrastructure.session_store import SessionStore
from video_downloader.infrastructure.settings import AppSettings
from video_downloader.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

#: Re-exported from `composition` because callers and tests have always
#: imported them from here.
__all__ = [
    "EXTRACTOR_MARKER",
    "REQUIRED_EXTRACTORS",
    "SMOKE_MARKER",
    "THIRD_PARTY_LOG_LEVELS",
    "WatchdogThread",
    "configure_logging",
    "create_job_runner",
    "create_provider_session",
    "create_qt_event_loop",
    "handle_asyncio_exception",
    "handle_exception",
    "handle_thread_exception",
    "run_application",
    "verify_extractors",
]


def create_qt_event_loop(application: QApplication) -> QSelectorEventLoop:
    """The Qt-driven asyncio loop the GUI runs on.

    Not `qasync.QEventLoop`, and deliberately so. On Windows that name resolves
    to `QIOCPEventLoop`, an `asyncio.ProactorEventLoop` subclass with no
    `add_reader`/`add_writer` - and curl_cffi drives libcurl through exactly
    those, so on the IOCP loop it answers by bolting a bridging selector thread
    onto the loop and sending every socket-readiness event across it. That
    bridge sits on the path every HLS segment travels. `QSelectorEventLoop`
    offers the interface curl_cffi asks for, so the bridge is never built.

    Off Windows the two names are the same class, so this is a Windows fix that
    changes nothing elsewhere. The CLI reaches the same loop kind through
    `infrastructure.event_loop.new_event_loop`.
    """
    return QSelectorEventLoop(application)


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
    # One store for the whole application: the window writes what a login
    # produced, every job's registry reads it back, and a job retrying right
    # after a login sees it because they share the instance that cached it.
    sessions = SessionStore()
    manager = DownloadManager(
        output_dir=settings.get_download_directory(),
        max_concurrent_downloads=3,
        job_runner=create_job_runner(partial(create_provider_session, sessions)),
    )
    window = MainWindow(manager, settings, sessions)

    if smoke_test:
        verify_extractors()
        print(EXTRACTOR_MARKER, flush=True)
        logger.info("Smoke test passed: Components constructed successfully")
        # Printed only in smoke mode, never during a normal start. The build reads
        # it to prove it exercised *this* artifact: an exit code alone cannot tell
        # a working executable from a stale one lying in another directory.
        print(SMOKE_MARKER, flush=True)
        return 0
        
    loop = create_qt_event_loop(application)
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
