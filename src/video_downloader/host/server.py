"""The core, listening on a loopback socket for exactly one front end.

Why a socket and not a pipe, given that the front end starts this process and
could have talked to it over stdin: the download engine drives libcurl through
`loop.add_reader`, which on Windows requires a selector loop, and a selector
loop on Windows can watch **sockets** and nothing else. A pipe would need its
own reader thread; `asyncio.start_server` needs nothing. The same code then
works unchanged on macOS and Linux.

Three properties of the binding, each deliberate:

* **`127.0.0.1` and port 0.** Loopback only, so nothing outside the machine can
  reach it, and an ephemeral port, so two copies never fight over one number.
* **A per-run token.** The port is guessable and any local process may connect
  to it; the token says which one was actually started by the front end. It is
  printed on stdout, which only the parent process can read.
* **One client.** A second connection is closed immediately. This is a core
  serving its own window, not a server: with two front ends, an ask would have
  no defined recipient and two lists would drift apart.

The handshake line is the only thing this process ever writes to stdout.
Logging goes to the file and to stderr, because a log line in the middle of the
stream would be indistinguishable from a message.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable

from video_downloader.application.download_manager import DownloadManager
from video_downloader.host import protocol
from video_downloader.host.asks import AskRegistry
from video_downloader.host.events import JobEventPublisher
from video_downloader.host.handlers import CommandError, CommandHandlers
from video_downloader.infrastructure.session_store import SessionStore
from video_downloader.infrastructure.settings import AppSettings

logger = logging.getLogger(__name__)

#: A line longer than this is not a message anybody meant to send. The cap is
#: generous - a job list of a few hundred entries fits easily - and exists so a
#: process that starts writing rubbish cannot exhaust memory here.
MAX_LINE_BYTES = 8 * 1024 * 1024


class Host:
    """One core, one socket, one front end at a time."""

    def __init__(
        self,
        *,
        manager: DownloadManager,
        settings: AppSettings,
        sessions: SessionStore,
        host: str = "127.0.0.1",
    ) -> None:
        self._manager = manager
        self._bind_host = host
        self._token = secrets.token_hex(16)
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._authenticated = False
        self._closed = asyncio.Event()

        self._asks = AskRegistry(self._send)
        self._publisher = JobEventPublisher(self._send)
        self._handlers = CommandHandlers(
            manager=manager,
            settings=settings,
            sessions=sessions,
            publisher=self._publisher,
            asks=self._asks,
            request_shutdown=self.request_shutdown,
        )
        self._dispatch: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            protocol.JOBS_LIST: self._handlers.jobs_list,
            protocol.JOBS_ADD: self._handlers.jobs_add,
            protocol.JOBS_CANCEL: self._handlers.jobs_cancel,
            protocol.JOBS_DELETE: self._handlers.jobs_delete,
            protocol.JOBS_RESCAN: self._handlers.jobs_rescan,
            protocol.SETTINGS_GET: self._handlers.settings_get,
            protocol.SETTINGS_SET_DIRECTORY: self._handlers.settings_set_directory,
            protocol.SESSIONS_LIST: self._handlers.sessions_list,
            protocol.SESSIONS_PUT: self._handlers.sessions_put,
            protocol.SESSIONS_CLEAR: self._handlers.sessions_clear,
            protocol.APP_SHUTDOWN: self._handlers.app_shutdown,
        }

    # --- lifecycle ---------------------------------------------------------

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        assert self._server is not None, "the host is not listening yet"
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._serve, self._bind_host, 0, limit=MAX_LINE_BYTES
        )
        logger.info("Host listening on %s:%d", self._bind_host, self.port)
        return self.port

    def request_shutdown(self) -> None:
        """Ask the run loop to end. Safe to call from a command handler."""
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def stop(self) -> None:
        """Stop listening, let go of every job, and end every open question."""
        self._asks.fail_all("the core is shutting down")
        self._publisher.detach_all()
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._manager.shutdown()

    # --- the connection ----------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._writer is not None:
            # One front end. A second is not queued, because it would silently
            # wait for a slot that only opens when the first one exits.
            logger.warning("Refusing a second front end")
            writer.close()
            return

        self._writer = writer
        self._authenticated = False
        logger.info("Front end connected")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                await self._handle_line(line)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            logger.info("Front end disconnected abruptly")
        except asyncio.LimitOverrunError:
            logger.warning("Front end sent a line past the size cap; closing")
        finally:
            self._writer = None
            self._authenticated = False
            # Both of these exist because the reader is gone: a job waiting on a
            # question would otherwise wait for its timeout, and every attached
            # job would go on serialising snapshots for nobody.
            self._asks.fail_all("the front end disconnected")
            self._publisher.detach_all()
            logger.info("Front end connection closed")
            writer.close()

    async def _handle_line(self, line: bytes) -> None:
        try:
            message = protocol.decode(line)
        except protocol.ProtocolError as error:
            logger.warning("Unreadable line: %s", error)
            self._send(protocol.failure(None, protocol.E_BAD_REQUEST, str(error)))
            return

        kind = message["type"]
        message_id = message.get("id")

        if kind == protocol.HELLO:
            self._hello(message)
            return

        if not self._authenticated:
            self._send(
                protocol.failure(message_id, protocol.E_UNAUTHENTICATED, "say hello first")
            )
            return

        if kind == protocol.ASK_REPLY:
            self._asks.resolve(str(message.get("askId") or ""), message.get("value"))
            return

        handler = self._dispatch.get(kind)
        if handler is None:
            self._send(
                protocol.failure(message_id, protocol.E_UNKNOWN_COMMAND, f"unknown: {kind}")
            )
            return

        try:
            data = await handler(message)
        except CommandError as error:
            self._send(protocol.failure(message_id, error.code, str(error)))
        except Exception as error:  # noqa: BLE001 - a bad command may not kill the core
            logger.exception("Command %s failed", kind)
            self._send(
                protocol.failure(message_id, protocol.E_FAILED, f"{type(error).__name__}: {error}")
            )
        else:
            self._send(protocol.result(message_id, data))

    def _hello(self, message: dict[str, Any]) -> None:
        """Check the token, and answer with everything the front end needs at once."""
        if not secrets.compare_digest(str(message.get("token") or ""), self._token):
            logger.warning("Front end presented the wrong token; closing")
            self._send(
                protocol.failure(message.get("id"), protocol.E_UNAUTHENTICATED, "bad token")
            )
            if self._writer is not None:
                self._writer.close()
            return

        self._authenticated = True
        jobs = self._manager.get_jobs()
        self._publisher.attach_all(jobs)
        self._send(
            protocol.result(
                message.get("id"),
                {
                    "protocol": protocol.PROTOCOL_VERSION,
                    "jobs": [protocol.job_payload(job) for job in jobs],
                },
            )
        )

    # --- sending -----------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> bool:
        """Write one message. Returns whether there was anybody to write to.

        The boolean is what makes "nobody is connected" answerable without an
        exception: `AskRegistry` reads it to decide that a question cannot be
        asked at all, which is a different situation from one that goes
        unanswered.
        """
        writer = self._writer
        if writer is None or writer.is_closing():
            return False
        try:
            writer.write(protocol.encode(message))
        except (ConnectionResetError, RuntimeError) as error:
            logger.debug("Could not write to the front end: %s", error)
            return False
        return True
