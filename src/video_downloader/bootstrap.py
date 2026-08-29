import asyncio
import faulthandler
import logging
import os
import platform
import sys
import threading
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from video_downloader.application.download_manager import DownloadManager
from video_downloader.ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

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

def configure_logging(debug: bool):
    handlers = []
    Path("runtime/downloads").mkdir(parents=True, exist_ok=True)
    Path("runtime/logs").mkdir(parents=True, exist_ok=True)
    
    if debug:
        file_handler = logging.FileHandler("runtime/logs/downloader-debug.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG)
        handlers.extend([file_handler, console_handler])
        level = logging.DEBUG
    else:
        file_handler = logging.FileHandler("runtime/logs/downloader.log", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        handlers.append(file_handler)
        level = logging.INFO
        
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] [%(threadName)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    for handler in handlers:
        handler.setFormatter(formatter)
        
    logging.basicConfig(level=level, handlers=handlers, force=True)

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
    
    # Use max_concurrent_downloads=3 (Phase 5)
    manager = DownloadManager(output_dir="runtime/downloads", max_concurrent_downloads=3)
    window = MainWindow(manager)

    if smoke_test:
        logger.info("Smoke test passed: Components constructed successfully")
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
