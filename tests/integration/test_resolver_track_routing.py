"""Which downloader fetches a resolver-resolved track, and why it is not ours.

The routing this pins used to go the other way. A one-byte probe proved whether
our engine transport could read a track to the end, and one that passed was
fetched by the engine rather than by the resolver - for the resume, retries and
atomic finalisation the engine owns and the resolver does not.

What that costs was never measured until a user reported a slow download. On one
YouTube track, on one machine, within the same few minutes:

    engine transport      556 KB/s
    yt-dlp's downloader   the whole 297 MiB in under 35 s, 12-27 MiB/s

So the routing is off, and these tests keep it off until somebody turns the
switch back on deliberately - at which point the last test here fails and says
so, which is the point of it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from base_api.models import MediaSource, MediaTrackInfo

from video_downloader.application import track_download
from video_downloader.application.track_download import (
    ROUTE_RESOLVER_TRACKS_TO_ENGINE,
    YTDLP_TRANSPORT,
    download_selection,
)
from video_downloader.application.track_selection import TrackSelection

PAGE = "https://provider.test/watch?v=abc"


def a_resolver_source(role: str | None = None, size: int | None = None) -> MediaSource:
    """A track only the resolver could have produced.

    `role=None` is a finished file - a source that states no role has meant that
    since before roles existed. A named role is one half of a pair.
    """
    return MediaSource(
        url=f"https://cdn.test/{role or 'combined'}.mp4",
        source_type=YTDLP_TRANSPORT,
        quality_value=2160,
        expected_size=size,
        identity=f"test:abc:{role or 'combined'}",
        track=MediaTrackInfo(
            role=role,
            container="mp4",
            video_codec="vp09.00.50.08" if role != "audio" else None,
            audio_codec="mp4a.40.2" if role == "audio" else None,
        ),
    )


@pytest.fixture
def watchers(monkeypatch):
    """Records which downloader was asked, and whether the probe was sent."""
    seen: dict[str, list] = {"resolver": [], "engine": [], "probe": []}

    async def probe(source, timeout: float = 15.0):
        seen["probe"].append(source.url)
        return 4096

    async def resolver(source, target, callback, stop_event, page_url):
        seen["resolver"].append(source.url)
        target.write_bytes(b"x" * 4096)
        callback(4096, 4096)

    async def engine(core, source, target, state_path, callback, stop_event):
        seen["engine"].append(source.url)
        target.write_bytes(b"x" * 4096)
        callback(4096, 4096)

    monkeypatch.setattr(track_download, "readable_total", probe)
    monkeypatch.setattr(track_download, "_download_via_resolver", resolver)
    monkeypatch.setattr(track_download, "_download_via_engine", engine)
    return seen


async def fetch(selection: TrackSelection, tmp_path: Path) -> Path | None:
    return await download_selection(
        selection,
        core=object(),
        target=tmp_path / "out.mp4",
        work_dir=tmp_path / "work",
        page_url=PAGE,
        stop_event=asyncio.Event(),
        report=lambda done, total: None,
    )


# --- where a resolver track goes ---------------------------------------------


@pytest.mark.asyncio
async def test_a_resolver_track_is_fetched_by_the_resolver(watchers, tmp_path):
    await fetch(TrackSelection(combined=a_resolver_source()), tmp_path)

    assert watchers["resolver"], "the resolver was not asked"
    assert watchers["engine"] == [], "the engine transport took the track"


@pytest.mark.asyncio
async def test_the_probe_is_not_even_sent(watchers, tmp_path):
    """It answers "can our transport finish this?", which nobody asks now.

    One request per track, and a fifteen-second timeout when a CDN does not
    answer it - paid for a decision that is no longer made.
    """
    await fetch(TrackSelection(combined=a_resolver_source()), tmp_path)

    assert watchers["probe"] == []


@pytest.mark.asyncio
async def test_a_sized_track_goes_the_same_way(watchers, tmp_path):
    """The routing never depended on the provider stating a size, and still does not."""
    await fetch(TrackSelection(combined=a_resolver_source(size=311_597_023)), tmp_path)

    assert watchers["resolver"] and watchers["engine"] == []


@pytest.mark.asyncio
async def test_both_tracks_of_a_pair_go_to_the_resolver(watchers, tmp_path, monkeypatch):
    monkeypatch.setattr(track_download, "mux_tracks", lambda *args, **kwargs: args[2])
    selection = TrackSelection(
        video=a_resolver_source("video"), audio=a_resolver_source("audio")
    )

    await fetch(selection, tmp_path)

    assert len(watchers["resolver"]) == 2
    assert watchers["engine"] == []


# --- and an ordinary HTTP source is untouched --------------------------------


@pytest.mark.asyncio
async def test_a_plain_http_track_still_uses_the_engine(watchers, tmp_path):
    """Only the resolver detour changed. A source the engine owns is still its own."""
    source = MediaSource(
        url="https://cdn.test/file.mp4",
        source_type="HTTP",
        expected_size=4096,
        track=MediaTrackInfo(role=None, container="mp4"),
    )

    await fetch(TrackSelection(combined=source), tmp_path)

    assert watchers["engine"], "the engine transport was bypassed"
    assert watchers["resolver"] == []


# --- the switch itself --------------------------------------------------------


def test_the_routing_is_off_and_says_why():
    """Fails the day somebody flips it back, which is when it must be re-measured.

    Not a tautology: it is the reminder that the switch carries a measurement,
    and that turning it on without repeating that measurement is how a fix
    becomes a regression again.
    """
    assert ROUTE_RESOLVER_TRACKS_TO_ENGINE is False
