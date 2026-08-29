import asyncio
import argparse
import logging
import signal

from video_downloader.domain.download_job import LifecycleState
from video_downloader.application.download_manager import DownloadManager


class ConsoleApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.manager = DownloadManager(args.output_dir)

    async def run(self) -> int:
        video_url = self.args.url or input("Bitte geben Sie die Video-URL ein: ").strip()
        if not video_url:
            print("FEHLER: Keine Video-URL angegeben.", flush=True)
            return 2

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
    parser = argparse.ArgumentParser(description="xHamster HLS console downloader")
    parser.add_argument("url", nargs="?", help="Video-URL; ohne Angabe interaktiv")
    parser.add_argument("-o", "--output-dir", default="downloads", help="Zielordner")
    parser.add_argument("-q", "--quality", default="best", help="Qualitaet, z. B. best, 720 oder worst")
    parser.add_argument("--no-remux", action="store_true", help="Transportstream nicht nach MP4 remuxen")
    return parser.parse_args()


def configure_logging() -> None:
    from pathlib import Path

    Path("downloads").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename="downloads/downloader.log")

def main() -> int:
    configure_logging()
    return asyncio.run(ConsoleApp(parse_args()).run())


if __name__ == "__main__":
    raise SystemExit(main())