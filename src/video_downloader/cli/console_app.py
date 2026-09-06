import argparse
import asyncio
import logging
import signal
from pathlib import Path

from video_downloader.application.download_manager import DownloadManager
from video_downloader.domain.download_job import LifecycleState
from video_downloader.infrastructure.event_loop import new_event_loop
from video_downloader.infrastructure.paths import AppPaths
from video_downloader.infrastructure.settings import AppSettings

NO_DIRECTORY_MESSAGE = (
    "FEHLER: Kein Download-Verzeichnis konfiguriert.\n"
    "Waehle eines in der GUI oder gib -o/--output-dir <Verzeichnis> an."
)


class ConsoleApp:
    def __init__(self, args: argparse.Namespace, settings: AppSettings | None = None) -> None:
        self.args = args
        self.settings = settings or AppSettings()
        self.manager: DownloadManager | None = None

    def resolve_output_directory(self) -> Path | None:
        """Explicit argument beats the saved setting; neither means we stop.

        The argument is an invocation-level override and is deliberately not
        persisted - a one-off `-o` should not silently rewrite the directory the
        GUI uses.
        """
        if self.args.output_dir:
            return Path(self.args.output_dir).expanduser()
        return self.settings.get_download_directory()

    async def run(self) -> int:
        directory = self.resolve_output_directory()
        if directory is None:
            # No GUI dialog here on purpose: the CLI must stay usable headless.
            print(NO_DIRECTORY_MESSAGE, flush=True)
            return 2

        video_url = self.args.url or input("Bitte geben Sie die Video-URL ein: ").strip()
        if not video_url:
            print("FEHLER: Keine Video-URL angegeben.", flush=True)
            return 2

        # The same providers the GUI uses, from the same composition root.
        # Imported here rather than at module level so that parsing arguments and
        # printing the usage errors above never pulls Qt in.
        from video_downloader.bootstrap import create_job_runner

        self.manager = DownloadManager(directory, job_runner=create_job_runner())
        job = self.manager.add_download(video_url, self.args.quality, remux=not self.args.no_remux)
        loop = asyncio.get_running_loop()
        try:
            for signal_name in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signal_name, job.request_stop)
                except (NotImplementedError, RuntimeError):
                    signal.signal(signal_name, lambda *_: job.request_stop())
            await job.asyncio_task
        finally:
            await self.manager.shutdown()
        return 0 if job.state == LifecycleState.COMPLETED else 130 if job.state == LifecycleState.CANCELLED else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HLS console downloader")
    parser.add_argument("url", nargs="?", help="Video-URL; ohne Angabe interaktiv")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Zielordner; ohne Angabe wird der gespeicherte Download-Ordner verwendet",
    )
    parser.add_argument("-q", "--quality", default="best", help="Qualitaet, z. B. best, 720 oder worst")
    parser.add_argument("--no-remux", action="store_true", help="Transportstream nicht nach MP4 remuxen")
    return parser.parse_args()


def configure_logging(paths: AppPaths | None = None) -> None:
    # Same per-user location the GUI uses, resolved through the same helper.
    log_dir = (paths or AppPaths.default()).ensure_log_dir()
    # UTF-8 explicitly, as the GUI's handler already does. Without it the file
    # is written in the console code page, and every error message carrying an
    # umlaut - which the provider messages do - lands in the log mangled.
    handler = logging.FileHandler(log_dir / "downloader-cli.log", encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> int:
    configure_logging()
    # Not asyncio's own default loop: on Windows that is a ProactorEventLoop,
    # which the curl_cffi transport cannot register its sockets on. See
    # infrastructure.event_loop.
    return asyncio.run(ConsoleApp(parse_args()).run(), loop_factory=new_event_loop)


if __name__ == "__main__":
    raise SystemExit(main())
