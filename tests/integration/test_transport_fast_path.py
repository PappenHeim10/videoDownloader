"""Routing a resolver-fetched track onto our own transport when that is safe.

The resolver's downloader works and is the fallback, but our transport is where
resume, retries and atomic finalisation live, so a track it can finish should go
through it. Whether it can is not a property of the provider - it is a property
of the individual URL, because some serve a fixed prefix and then refuse.

One request for one byte answers that, and these tests pin both directions: the
routing happens when the last byte is readable, and it does not when it is not,
when the size is unknown, or when the probe itself fails. A probe must never be
the reason a job fails - the path it would fall back to already works.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Iterator

import pytest
from base_api.models import MediaSource, MediaTrackInfo

from video_downloader.application.track_download import (
    YTDLP_TRANSPORT,
    as_engine_source,
    can_engine_read_whole,
)

BODY = bytes(range(256)) * 40  # 10240 bytes, deterministic


@contextmanager
def serving(readable_prefix: int | None = None, status: int = 206) -> Iterator[str]:
    """A server that answers ranges, optionally only within a prefix."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - the base class names it
            header = self.headers.get("Range")
            start = int(header.split("=", 1)[1].split("-", 1)[0]) if header else 0
            if readable_prefix is not None and start >= readable_prefix:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = int(header.split("-", 1)[1] or len(BODY) - 1) if header else len(BODY) - 1
            payload = BODY[start:end + 1]
            self.send_response(status)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def source_for(base_url: str, *, size: int | None = len(BODY)) -> MediaSource:
    return MediaSource(
        url=f"{base_url}/track.m4a",
        source_type=YTDLP_TRANSPORT,
        expected_size=size,
        identity="youtube:abcdefghijk:140",
        track=MediaTrackInfo(role="audio", container="m4a", audio_codec="mp4a.40.2"),
    )


@pytest.mark.asyncio
async def test_a_fully_readable_track_is_routed_onto_our_transport():
    with serving() as base_url:
        assert await can_engine_read_whole(source_for(base_url)) is True


@pytest.mark.asyncio
async def test_a_prefix_limited_track_stays_on_the_resolver():
    """The case the probe exists for: bytes stop partway, with no warning."""
    with serving(readable_prefix=len(BODY) // 2) as base_url:
        assert await can_engine_read_whole(source_for(base_url)) is False


@pytest.mark.asyncio
async def test_a_track_of_unknown_size_cannot_be_probed():
    with serving() as base_url:
        assert await can_engine_read_whole(source_for(base_url, size=None)) is False
        assert await can_engine_read_whole(source_for(base_url, size=0)) is False


@pytest.mark.asyncio
async def test_a_server_that_ignores_the_range_is_not_trusted():
    """A 200 means the whole body, not the byte we asked for."""
    with serving(status=200) as base_url:
        assert await can_engine_read_whole(source_for(base_url)) is False


@pytest.mark.asyncio
async def test_a_probe_that_cannot_connect_answers_no_rather_than_raising():
    unreachable = MediaSource(
        url="http://127.0.0.1:9/track.m4a",
        source_type=YTDLP_TRANSPORT,
        expected_size=1024,
    )

    assert await can_engine_read_whole(unreachable, timeout=2.0) is False


@pytest.mark.asyncio
async def test_the_probe_costs_one_byte():
    served: list[str | None] = []

    class Counting(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            served.append(self.headers.get("Range"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {len(BODY)-1}-{len(BODY)-1}/{len(BODY)}")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(BODY[-1:])

    server = HTTPServer(("127.0.0.1", 0), Counting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        await can_engine_read_whole(source_for(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)

    assert served == [f"bytes={len(BODY) - 1}-{len(BODY) - 1}"]


def test_routing_copies_the_source_rather_than_rewriting_it():
    """The selection is shared; changing it would rewrite what the job recorded."""
    original = source_for("http://example.test")

    routed = as_engine_source(original)

    assert routed.source_type == "HTTP"
    assert original.source_type == YTDLP_TRANSPORT
    assert routed.url == original.url
    assert routed.identity == original.identity
    assert routed.track.audio_codec == original.track.audio_codec


def test_the_routed_source_keeps_the_identity_that_makes_resume_work():
    routed = as_engine_source(source_for("http://example.test"))

    assert routed.identity == "youtube:abcdefghijk:140"
