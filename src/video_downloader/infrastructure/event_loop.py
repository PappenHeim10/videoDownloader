"""The kind of asyncio event loop this application's transport needs.

Every byte the application fetches goes through curl_cffi, which drives libcurl
by registering its sockets with `loop.add_reader()` / `add_writer()`. Windows
offers no default loop that implements them: plain asyncio hands back a
`ProactorEventLoop`, and qasync's `QEventLoop` resolves there to
`QIOCPEventLoop`, a subclass of that same proactor loop. On either, curl_cffi
starts a bridging selector thread and routes every socket-readiness event
through it - an extra thread and a cross-thread hop on the path that carries
every HLS segment, in the GUI and the CLI alike.

So the loop kind is an application decision, not a per-entry-point default. This
module holds it for everything that builds a plain asyncio loop - the CLI and
the test suite. The GUI needs a Qt-driven loop and so cannot use it;
`bootstrap.create_qt_event_loop` makes the same choice in qasync's terms.

Deliberately free of Qt, like the rest of this package.

What a selector loop costs on Windows: `select()` tops out at 512 sockets, and
asyncio subprocesses are unavailable. Neither binds here - a handful of
concurrent downloads stays far below that ceiling, and the application starts no
subprocess.
"""

from __future__ import annotations

import asyncio
import sys


def new_event_loop() -> asyncio.AbstractEventLoop:
    """Create a loop that implements the `add_reader` family curl_cffi needs."""
    if sys.platform == "win32":
        # asyncio.new_event_loop() would hand back a ProactorEventLoop here.
        return asyncio.SelectorEventLoop()
    # Every other platform already defaults to a selector loop.
    return asyncio.new_event_loop()
