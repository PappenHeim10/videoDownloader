"""Starting the core the way a front end will actually start it.

The conformance tests drive a `Host` object inside the test process. This one
starts `python -m video_downloader.host` as a child, reads its handshake off
stdout and connects to the port it names - the whole sequence a front end has to
get right before any of the protocol matters.

Three things are only testable from out here:

* that the handshake is on **stdout** and is the only thing there,
* that the port it names is really listening,
* that `app.shutdown` ends the process rather than merely closing a socket.

Deliberately written without asyncio. `asyncio.create_subprocess_exec` raises
`NotImplementedError` on a Windows selector loop, and a selector loop is what
this application runs on everywhere - see `infrastructure/event_loop.py`. That
is not a problem for the design, because the front end is the parent process and
starts the core with its own runtime's tools; it only means a Python test has to
use `subprocess` and a plain socket, which is also closer to what a front end
written in something else will do.

Slower than the in-process tests because it starts an interpreter and imports
yt-dlp with it, so it is one walk through the sequence rather than a case per
assertion.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest

from video_downloader.host import protocol

#: Generous: the child imports yt-dlp, base_api and curl_cffi before it binds.
STARTUP_TIMEOUT = 120.0


class Line:
    """Newline-delimited reading over a blocking socket."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = b""

    def send(self, message: dict) -> None:
        self._connection.sendall(protocol.encode(message))

    def receive(self) -> dict:
        while b"\n" not in self._buffer:
            chunk = self._connection.recv(65536)
            assert chunk, "the core closed the connection"
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return json.loads(line.decode("utf-8"))


@pytest.fixture
def child_environment(tmp_path):
    """An environment that touches no real per-user directory."""
    environment = os.environ.copy()
    environment["VIDEO_DOWNLOADER_HOME"] = str(tmp_path / "appdata")
    return environment


@pytest.mark.timeout(240)
def test_the_core_starts_serves_and_shuts_down(child_environment):
    process = subprocess.Popen(
        [sys.executable, "-m", "video_downloader.host"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=child_environment,
    )
    try:
        line = process.stdout.readline()
        assert line, "the core exited before printing a handshake"
        handshake = json.loads(line.decode("utf-8"))

        assert handshake["protocol"] == protocol.PROTOCOL_VERSION
        assert isinstance(handshake["port"], int) and handshake["port"] > 0
        assert len(handshake["token"]) >= 32

        connection = socket.create_connection(("127.0.0.1", handshake["port"]), timeout=30)
        try:
            stream = Line(connection)
            stream.send({"type": protocol.HELLO, "id": 1, "token": handshake["token"]})
            answer = stream.receive()

            assert answer["ok"], answer.get("error")
            assert answer["data"]["protocol"] == protocol.PROTOCOL_VERSION
            assert answer["data"]["jobs"] == []

            stream.send({"type": protocol.APP_SHUTDOWN, "id": 2})
        finally:
            connection.close()

        assert process.wait(timeout=60) == 0
        # Nothing else ever reached stdout: what is left after the handshake is
        # end of file, which only happens because the process exited.
        assert process.stdout.read() == b"", "the core wrote past its handshake"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        process.stdout.close()


@pytest.mark.timeout(240)
def test_a_client_without_the_token_gets_nowhere(child_environment):
    """The port is guessable; the token is what says who started this core."""
    process = subprocess.Popen(
        [sys.executable, "-m", "video_downloader.host"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=child_environment,
    )
    try:
        handshake = json.loads(process.stdout.readline().decode("utf-8"))
        connection = socket.create_connection(("127.0.0.1", handshake["port"]), timeout=30)
        try:
            stream = Line(connection)
            stream.send({"type": protocol.HELLO, "id": 1, "token": "guessed"})
            answer = stream.receive()

            assert answer["ok"] is False
            assert answer["error"]["code"] == protocol.E_UNAUTHENTICATED
        finally:
            connection.close()
    finally:
        process.kill()
        process.wait(timeout=30)
        process.stdout.close()
