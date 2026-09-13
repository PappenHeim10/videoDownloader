"""Start the core without a window: `python -m video_downloader.host`.

The whole process is three steps. Build the same components `bootstrap` builds
for the window - settings, session store, download manager with the production
providers - start the socket, and print one line saying where it is.

Nothing else reaches stdout. `configure_logging` is given stderr for its console
handler, because a log line written into the stream a front end is parsing is a
protocol violation that would look like a malformed message.

The loop comes from `infrastructure.event_loop`, the same one the CLI uses and
for the same reason: curl_cffi registers its sockets with `add_reader`, which a
Windows proactor loop does not have.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from functools import partial

from video_downloader.application.download_manager import DownloadManager
from video_downloader.composition import (
    configure_logging,
    create_job_runner,
    create_provider_session,
    handle_asyncio_exception,
    handle_exception,
    handle_thread_exception,
)
from video_downloader.host import protocol
from video_downloader.host.server import Host
from video_downloader.infrastructure.event_loop import new_event_loop
from video_downloader.infrastructure.session_store import SessionStore
from video_downloader.infrastructure.settings import AppSettings

logger = logging.getLogger(__name__)

#: Printed on stdout by `--self-test` instead of the handshake. Proves the host
#: can be constructed and can bind, without leaving a process behind - the same
#: thing `--smoke-test` proves for the window.
SELF_TEST_MARKER = "VideoDownloader host OK"


def build_host() -> Host:
    """The production core, composed exactly as the window composes it."""
    settings = AppSettings()
    sessions = SessionStore()
    manager = DownloadManager(
        output_dir=settings.get_download_directory(),
        max_concurrent_downloads=3,
        job_runner=create_job_runner(partial(create_provider_session, sessions)),
    )
    return Host(manager=manager, settings=settings, sessions=sessions)


async def serve(*, self_test: bool = False) -> int:
    host = build_host()
    port = await host.start()

    if self_test:
        await host.stop()
        print(SELF_TEST_MARKER, flush=True)
        return 0

    # The one line on stdout. Flushed immediately: the front end is blocked on
    # reading it, and a buffered handshake is a deadlock.
    print(protocol.handshake(port, host.token), flush=True)

    try:
        await host.wait_closed()
    finally:
        await host.stop()
    logger.info("Host stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Video downloader core, without a window")
    parser.add_argument("--debug", action="store_true", help="verbose logging on stderr")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="construct and bind, then exit - proves the start path",
    )
    args = parser.parse_args()

    configure_logging(args.debug, stream=sys.stderr)
    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception

    return asyncio.run(_serve(args), loop_factory=new_event_loop)


async def _serve(args: argparse.Namespace) -> int:
    # Set on the running loop rather than before it exists, so an unretrieved
    # task exception reaches the log instead of asyncio's default warning.
    asyncio.get_running_loop().set_exception_handler(handle_asyncio_exception)
    return await serve(self_test=args.self_test)


if __name__ == "__main__":
    raise SystemExit(main())
