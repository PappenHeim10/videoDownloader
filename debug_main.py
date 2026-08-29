"""Debug entry point for the frozen build.

Kept deliberately thin. The only thing that happens before `video_downloader` is
imported is the crash reporter below, because a failure to import the application
package is exactly the failure that has to stay diagnosable - and at that point
nothing from the application is available to report it.
"""

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _crash_log_path() -> Path:
    """Where to persist a startup traceback.

    A miniature of AppPaths on purpose: importing video_downloader.infrastructure
    is precisely what may have just failed, so this cannot depend on it. Kept in
    sync by the test that asserts both resolve to the same directory.
    """
    override = os.environ.get("VIDEO_DOWNLOADER_HOME")
    if override:
        root = Path(override)
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        root = (Path(local) if local else Path.home() / "AppData" / "Local") / "VideoDownloader"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "VideoDownloader"
    else:
        root = Path.home() / ".local" / "share" / "VideoDownloader"
    return root / "logs" / "startup-crash.log"


def report_startup_failure(error: BaseException) -> None:
    """Put a fatal startup error somewhere it survives the console closing.

    Double-clicking the Debug executable opens a console that disappears with the
    process, so stderr alone is unreadable in the one situation where it matters
    most.
    """
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    sys.stderr.write(text)
    sys.stderr.flush()

    try:
        path = _crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====\n")
            handle.write(f"frozen={getattr(sys, 'frozen', False)} executable={sys.executable}\n")
            handle.write(text)
    except OSError:
        # Nothing sensible left to do - the traceback is already on stderr, and
        # failing to log a crash must not replace it with a different crash.
        pass


try:
    from video_downloader.bootstrap import run_application
except Exception as error:  # noqa: BLE001 - last line of defence before exit
    report_startup_failure(error)
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args, _ = parser.parse_known_args()
    try:
        code = run_application(debug=True, smoke_test=args.smoke_test)
    except Exception as error:  # noqa: BLE001 - same reasoning as above
        report_startup_failure(error)
        raise SystemExit(1)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
