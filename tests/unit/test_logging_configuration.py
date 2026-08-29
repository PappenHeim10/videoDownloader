"""Debug logging must stay about the application, not about the frameworks.

A real Debug download emitted raw segment payloads - `b"\\xe9\\x77\\x9e..."` -
straight into the log file. The source was qasync's executor, which logs every
callback it dispatches together with its arguments; the HLS writer is dispatched
as write_part(path, data), so `data` is an entire segment. Turning on application
DEBUG had turned that on too.
"""

from __future__ import annotations

import logging

import pytest

from video_downloader.bootstrap import THIRD_PARTY_LOG_LEVELS, configure_logging
from video_downloader.infrastructure.paths import HOME_ENV_VAR


@pytest.fixture(autouse=True)
def isolated_logging(tmp_path, monkeypatch):
    """Configure logging into a throwaway location and put it back afterwards."""
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    watched = {name: logging.getLogger(name).level for name in THIRD_PARTY_LOG_LEVELS}
    yield
    for handler in list(root.handlers):
        if handler not in before_handlers:
            handler.close()
    root.handlers, root.level = before_handlers, before_level
    for name, level in watched.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("name", sorted(THIRD_PARTY_LOG_LEVELS))
def test_framework_internals_stay_quiet_in_debug_mode(name):
    configure_logging(debug=True)
    assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)


def test_application_debug_logging_is_still_available():
    configure_logging(debug=True)

    for name in ("video_downloader", "video_downloader.application.download_service"):
        assert logging.getLogger(name).isEnabledFor(logging.DEBUG)


def test_a_segment_payload_dispatched_through_qasync_is_not_written():
    """The exact shape that produced the megabyte log lines."""
    configure_logging(debug=True)

    captured: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Collector()
    logging.getLogger().addHandler(handler)
    try:
        payload = b"\xe9\x77\x9eSEGMENT-PAYLOAD-SENTINEL\x00\xff" * 64
        qasync_logger = logging.getLogger("qasync")
        # Verbatim the call qasync makes for every dispatched callback.
        qasync_logger.debug(
            "#%s got callback %s with args %s and kwargs %s from queue",
            1,
            "write_part",
            ("segment.tmp", payload),
            {},
        )
        qasync_logger.debug("Setting Future result: %s", payload)
    finally:
        logging.getLogger().removeHandler(handler)

    assert "SEGMENT-PAYLOAD-SENTINEL" not in "\n".join(captured)
    assert captured == []


def test_application_warnings_from_frameworks_still_get_through():
    # Muting DEBUG must not mute a genuine problem.
    configure_logging(debug=True)
    assert logging.getLogger("qasync").isEnabledFor(logging.WARNING)
    assert logging.getLogger("asyncio").isEnabledFor(logging.WARNING)


def test_the_levels_also_apply_outside_debug_mode():
    configure_logging(debug=False)
    for name in THIRD_PARTY_LOG_LEVELS:
        assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
