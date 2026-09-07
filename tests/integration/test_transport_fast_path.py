"""Routing a resolver-fetched track onto our own transport when that is safe.

The resolver's downloader works and is the fallback, but our transport is where
resume, retries and atomic finalisation live, so a track it can finish should go
through it. Whether it can is not a property of the provider - it is a property
of the individual URL, because some serve a fixed prefix and then refuse.

One request for one byte answers that, and these tests pin both directions: the
routing happens when the last byte is readable, and it does not when it is not
or when the probe itself fails. A probe must never be the reason a job fails -
the path it would fall back to already works.

The same request answers a second question for a track nobody sized, which is
every track X publishes: asked as a suffix range, the 206 states the length. So
the probe reports a total rather than a yes, and the tests below pin what may be
read as one - the byte served has to be the resource's last, or the number is
not an answer to what was asked.
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
    readable_total,
)

BODY = bytes(range(256)) * 40  # 10240 bytes, deterministic


def span_of(header: str | None, size: int) -> tuple[int, int]:
    """The byte range a `Range` header asks for, suffix form included.

    `bytes=5-9`, `bytes=5-`, and the form the probe uses when nobody stated a
    size: `bytes=-1`, which asks for the last byte without naming it.
    """
    if not header:
        return 0, size - 1
    first, _, last = header.split("=", 1)[1].partition("-")
    if not first:
        return size - int(last), size - 1
    return int(first), int(last) if last else size - 1


@contextmanager
def serving(readable_prefix: int | None = None, status: int = 206) -> Iterator[str]:
    """A server that answers ranges, optionally only within a prefix."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - the base class names it
            start, end = span_of(self.headers.get("Range"), len(BODY))
            if readable_prefix is not None and start >= readable_prefix:
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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
        assert await readable_total(source_for(base_url)) == len(BODY)


@pytest.mark.asyncio
async def test_a_prefix_limited_track_stays_on_the_resolver():
    """The case the probe exists for: bytes stop partway, with no warning."""
    with serving(readable_prefix=len(BODY) // 2) as base_url:
        assert await readable_total(source_for(base_url)) is None


@pytest.mark.asyncio
async def test_a_track_nobody_sized_has_its_length_measured():
    """Every track X publishes: no `filesize`, and no size worth deriving.

    The suffix range asks for the last byte without knowing which one it is, and
    the answer states the length - so a track that used to fall back to the
    resolver for want of a number now reaches our transport with the real one.
    """
    with serving() as base_url:
        assert await readable_total(source_for(base_url, size=None)) == len(BODY)
        assert await readable_total(source_for(base_url, size=0)) == len(BODY)


@pytest.mark.asyncio
async def test_a_track_nobody_sized_that_stops_partway_stays_on_the_resolver():
    """The measurement may not cost the guarantee it replaced."""
    with serving(readable_prefix=len(BODY) // 2) as base_url:
        assert await readable_total(source_for(base_url, size=None)) is None


@pytest.mark.asyncio
async def test_a_server_that_ignores_the_range_is_not_trusted():
    """A 200 means the whole body, not the byte we asked for."""
    with serving(status=200) as base_url:
        assert await readable_total(source_for(base_url)) is None
        assert await readable_total(source_for(base_url, size=None)) is None


@pytest.mark.asyncio
async def test_a_suffix_range_answered_with_some_other_byte_states_no_length():
    """A 206 about the wrong byte is not an answer to what was asked.

    The header is where the length comes from, so it is checked rather than
    read: a server answering "the last byte" with its first one has proved
    nothing about the end of the file, which is the whole point of the request.
    """

    class Misleading(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-0/{len(BODY)}")
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(BODY[:1])

    server = HTTPServer(("127.0.0.1", 0), Misleading)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert await readable_total(source_for(base_url, size=None)) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.asyncio
async def test_a_probe_that_cannot_connect_answers_no_rather_than_raising():
    unreachable = MediaSource(
        url="http://127.0.0.1:9/track.m4a",
        source_type=YTDLP_TRANSPORT,
        expected_size=1024,
    )

    assert await readable_total(unreachable, timeout=2.0) is None


@pytest.mark.parametrize(
    ("size", "expected_range"),
    [
        (len(BODY), f"bytes={len(BODY) - 1}-{len(BODY) - 1}"),
        (None, "bytes=-1"),
    ],
)
@pytest.mark.asyncio
async def test_the_probe_costs_one_byte(size, expected_range):
    """One request either way - by number when the size is stated, else suffix."""
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
        base_url = f"http://127.0.0.1:{server.server_port}"
        assert await readable_total(source_for(base_url, size=size)) == len(BODY)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)

    assert served == [expected_range]


def test_routing_copies_the_source_rather_than_rewriting_it():
    """The selection is shared; changing it would rewrite what the job recorded."""
    original = source_for("http://example.test")

    routed = as_engine_source(original, len(BODY))

    assert routed.source_type == "HTTP"
    assert original.source_type == YTDLP_TRANSPORT
    assert routed.url == original.url
    assert routed.identity == original.identity
    assert routed.track.audio_codec == original.track.audio_codec


def test_the_routed_source_carries_the_length_the_probe_measured():
    """The engine resumes and finishes against this number, so it has to be there."""
    original = source_for("http://example.test", size=None)

    routed = as_engine_source(original, len(BODY))

    assert routed.expected_size == len(BODY)
    assert original.expected_size is None


def test_the_routed_source_keeps_the_identity_that_makes_resume_work():
    routed = as_engine_source(source_for("http://example.test"), len(BODY))

    assert routed.identity == "youtube:abcdefghijk:140"
