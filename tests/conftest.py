"""Suite-wide asyncio setup.

These tests run the real transport: `create_provider_session()` builds the
production registry, and the `XHamsterAdapter` it registers owns a live
`curl_cffi.AsyncSession`. So the loop the suite runs on has to be the loop the
application runs on, or the suite would be proving the transport works on a loop
no entry point uses.

`pytest_asyncio_loop_factories` is how pytest-asyncio takes a loop factory, and
is the supported replacement for overriding the `event_loop_policy` fixture.
Returning exactly one factory leaves the test ids untouched - pytest-asyncio
only appends a factory name when it has more than one to choose between.

Tests that drive `asyncio.run()` themselves are not covered by the hook and pass
`loop_factory=new_event_loop` at the call site instead.
"""

from __future__ import annotations

from video_downloader.infrastructure.event_loop import new_event_loop


def pytest_asyncio_loop_factories(config, item):
    return {"app": new_event_loop}
