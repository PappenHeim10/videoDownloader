"""The event loop is an application decision, not a platform default.

curl_cffi drives libcurl by registering its sockets with `loop.add_reader()`,
and neither default this application used to take implements that method. Plain
asyncio hands back a `ProactorEventLoop` on Windows, and `qasync.QEventLoop`
resolves there to `QIOCPEventLoop`, a subclass of the same. On both, curl_cffi
answered by bolting a bridging selector thread onto the loop and routing every
socket-readiness event across it - on the path every HLS segment travels, in the
GUI and the CLI alike. It said so in a warning that only the test run ever read.

Both entry points now name the loop they want. These tests pin that at each end.
The CLI one matters most: `loop_factory=` is a single keyword argument that a
refactor can drop without anything failing except a warning nobody reads.
"""

from __future__ import annotations

import asyncio
import sys
import warnings

import pytest
from curl_cffi.utils import CurlCffiWarning

from video_downloader.bootstrap import create_provider_session, create_qt_event_loop
from video_downloader.cli import console_app
from video_downloader.infrastructure.event_loop import new_event_loop

#: Empty off Windows, where no proactor loop exists and there was never anything
#: to fix; `isinstance(x, ())` is False, so the assertions below simply hold.
PROACTOR = getattr(asyncio, "ProactorEventLoop", ())


def test_the_plain_asyncio_loop_is_not_the_one_the_platform_defaults_to():
    loop = new_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
        assert not isinstance(loop, PROACTOR)
    finally:
        loop.close()


def test_the_gui_loop_is_not_the_one_qasync_defaults_to(qapp):
    loop = create_qt_event_loop(qapp)
    try:
        assert not isinstance(loop, PROACTOR)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows has a proactor default")
def test_the_defaults_this_replaces_really_are_proactor_loops():
    """Without this, the two tests above would pass on a revert and prove nothing."""
    import qasync

    assert issubclass(qasync.QEventLoop, asyncio.ProactorEventLoop)
    default = asyncio.new_event_loop()
    try:
        assert isinstance(default, asyncio.ProactorEventLoop)
    finally:
        default.close()


def test_a_real_provider_session_closes_without_curl_cffi_building_a_bridge():
    """The concrete symptom: closing a never-used session materialises an AsyncCurl.

    `XHamsterAdapter` opens its `curl_cffi.AsyncSession` eagerly, and closing one
    that never made a request still constructs the curl multi handle - which is
    where curl_cffi inspects the loop and decides whether it needs a bridge.
    """
    session = create_provider_session()

    with warnings.catch_warnings(record=True) as raised:
        warnings.simplefilter("always")
        asyncio.run(session.close(), loop_factory=new_event_loop)

    assert [w.message for w in raised if issubclass(w.category, CurlCffiWarning)] == []


def test_the_cli_entry_point_runs_on_the_application_loop(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["video-downloader", "https://cdn.example/a.m3u8"])
    # Logging is not what this test is about, and reconfiguring the root logger
    # from inside the suite is rude to every other test - see the note on
    # _captured_log_targets in tests/unit/test_settings_and_paths.py.
    monkeypatch.setattr(console_app, "configure_logging", lambda: None)

    recorded: dict = {}

    def record(coroutine, **kwargs):
        coroutine.close()  # nothing is downloaded here; only the loop choice is under test
        recorded.update(kwargs)
        return 0

    monkeypatch.setattr(console_app.asyncio, "run", record)

    assert console_app.main() == 0
    assert recorded.get("loop_factory") is new_event_loop
