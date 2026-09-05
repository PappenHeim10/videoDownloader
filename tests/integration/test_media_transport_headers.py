"""A2: media transport requirements travel on the MediaSource, not on a session.

Before this, xHamster downloads worked only because the adapter's `Client` had
mutated the shared job session (`Referer`), and every other source downloaded
over that session silently inherited the header. Now:

* extraction runs on the adapter's own client (its session state stays there),
* the job's download core is provider-clean,
* `MediaSource.headers` carries what the media requests must send, and the real
  engine applies it per request - master manifest, media playlist and every
  segment alike.

These tests drive the actual application service (`run_download_job`) over a
real `BaseCore` whose network layer is replaced by a recorder, so the full
engine - playlist parsing, segment pool, assembly, state files - runs without
any network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from base_api import DirectMediaAdapter
from base_api.base import BaseCore
from base_api.models import Media, MediaSource
from base_api.modules.config import RuntimeConfig
from xhamster_api.xhamster_api import Short, Video

from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob, LifecycleState

XHAMSTER_REFERER = {"Referer": "https://www.xhamster.com/"}

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
    "media_360.m3u8\n"
)
MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:4\n"
    "#EXTINF:4.0,\n"
    "seg0.ts\n"
    "#EXTINF:4.0,\n"
    "seg1.ts\n"
    "#EXT-X-ENDLIST\n"
)


def recording_core():
    """A real BaseCore; only the two network calls are replaced by recorders."""
    core = BaseCore(RuntimeConfig())
    requests: list[tuple[str, dict | None]] = []

    async def fake_fetch_text(url=None, **kwargs):
        await asyncio.sleep(0)
        requests.append((url, kwargs.get("headers")))
        return MASTER if "master" in url else MEDIA_PLAYLIST

    async def fake_fetch_bytes(url, **kwargs):
        await asyncio.sleep(0)
        requests.append((url, kwargs.get("headers")))
        return b"SEGMENTBYTES"

    core.fetch_text = fake_fetch_text
    core.fetch_bytes = fake_fetch_bytes
    return core, requests


class OneMediaRegistry:
    def __init__(self, media: Media):
        self.media = media

    async def resolve(self, url: str) -> Media:
        return self.media

    async def close(self) -> None:
        return None


def media_on(host: str, title: str, headers: dict | None = None) -> Media:
    return Media(
        provider="test",
        original_url=f"https://{host}/watch",
        title=title,
        sources=[
            MediaSource(
                url=f"https://{host}/master.m3u8",
                source_type="HLS",
                headers=dict(headers) if headers else {},
            )
        ],
    )


async def run_job(media: Media, core: BaseCore, tmp_path: Path) -> DownloadJob:
    # remux=False: the recorded segments are not real TS packets, so the job
    # takes the pass-through finalize. Everything before that - playlist
    # resolution, segment pool, assembly, state files - is the real engine.
    job = DownloadJob(
        url=media.original_url, quality="best", output_dir=tmp_path, remux=False
    )
    session = ProviderSession(registry=OneMediaRegistry(media), core=core)
    await run_download_job(job, session_factory=lambda: session)
    return job


# --- the app path applies the source's headers to every media request --------


@pytest.mark.asyncio
async def test_source_headers_reach_manifest_playlist_and_all_segments(tmp_path):
    core, requests = recording_core()
    media = media_on("cdn.a", "Transport Check", XHAMSTER_REFERER)

    job = await run_job(media, core, tmp_path)

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file == tmp_path / "Transport Check.mp4"
    assert job.output_file.read_bytes() == b"SEGMENTBYTES" * 2

    assert [url for url, _ in requests] == [
        "https://cdn.a/master.m3u8",      # master manifest
        "https://cdn.a/media_360.m3u8",   # chosen media playlist
        "https://cdn.a/seg0.ts",          # every segment
        "https://cdn.a/seg1.ts",
    ]
    assert all(headers == XHAMSTER_REFERER for _, headers in requests)


@pytest.mark.asyncio
async def test_a_headerless_source_sends_no_source_headers(tmp_path):
    core, requests = recording_core()
    media = media_on("cdn.plain", "Plain Direct")

    job = await run_job(media, core, tmp_path)

    assert job.state == LifecycleState.COMPLETED
    # The emptiness check is its own assertion: without it, `all()` over an empty
    # recorder would pass and the test would prove nothing.
    assert requests
    assert all(not headers for _, headers in requests)


@pytest.mark.asyncio
async def test_a_headered_job_does_not_leak_into_a_later_plain_one(tmp_path):
    """Item 19: worst case, both jobs on one core. Source A carries the xHamster
    Referer, source B carries nothing - B must not inherit it."""
    core, requests = recording_core()

    job_a = await run_job(media_on("cdn.a", "With Referer", XHAMSTER_REFERER), core, tmp_path)
    job_b = await run_job(media_on("cdn.b", "Without Referer"), core, tmp_path)

    assert job_a.state == LifecycleState.COMPLETED
    assert job_b.state == LifecycleState.COMPLETED

    a_headers = [headers for url, headers in requests if "cdn.a" in url]
    b_headers = [headers for url, headers in requests if "cdn.b" in url]

    assert a_headers
    assert all(headers == XHAMSTER_REFERER for headers in a_headers)
    assert b_headers
    assert all(not headers for headers in b_headers)


@pytest.mark.asyncio
async def test_two_concurrent_jobs_keep_their_own_transport_headers(tmp_path):
    """Item 20: concurrent downloads with different headers, no cross-talk."""
    core, requests = recording_core()
    referer_b = {"Referer": "https://b.example/"}

    job_a, job_b = await asyncio.gather(
        run_job(media_on("cdn.a", "Job A", XHAMSTER_REFERER), core, tmp_path),
        run_job(media_on("cdn.b", "Job B", referer_b), core, tmp_path),
    )

    assert job_a.state == LifecycleState.COMPLETED
    assert job_b.state == LifecycleState.COMPLETED

    for url, headers in requests:
        expected = XHAMSTER_REFERER if "cdn.a" in url else referer_b
        assert headers == expected, f"{url} carried {headers}"


# --- provider mappings ---------------------------------------------------------


def test_xhamster_video_source_carries_the_media_request_headers():
    video = Video(url="https://xhamster.com/videos/example-1", core=None)
    video.__dict__["title"] = "A title"
    video.__dict__["pornstars"] = []
    video.__dict__["thumbnail"] = "https://img.example/thumb.jpg"
    video.__dict__["m3u8_base_url"] = "https://cdn.example/x.m3u8"

    source = video.to_media().sources[0]

    assert source.source_type == "HLS"
    assert source.headers == XHAMSTER_REFERER


def test_xhamster_short_source_carries_the_media_request_headers():
    short = Short(url="https://xhamster.com/moments/example-1", core=None)
    short.__dict__["video_id"] = 1
    short.__dict__["title"] = "A short"
    short.__dict__["author"] = ""
    short.__dict__["duration"] = 0
    short.__dict__["tags"] = []
    short.__dict__["poster_url"] = ""
    short.__dict__["thumb_url"] = "https://img.example/t.jpg"
    short.__dict__["m3u8_base_url"] = "https://cdn.example/s.m3u8"

    source = short.to_media().sources[0]

    assert source.source_type == "HLS"
    assert source.headers == XHAMSTER_REFERER


@pytest.mark.asyncio
async def test_a_direct_source_has_no_provider_headers():
    """Item 17: the guard against the original leak - a raw .m3u8 resolved in an
    application that also registers XHamsterAdapter carries no xHamster headers."""
    media = await DirectMediaAdapter().resolve("https://cdn.example/live/master.m3u8")

    assert media.sources[0].headers == {}


# --- the model, as the application relies on it --------------------------------


def test_sources_do_not_share_header_state():
    template = {"Referer": "https://a.example/"}
    first = MediaSource(url="https://cdn/1.m3u8", source_type="HLS", headers=template)
    second = MediaSource(url="https://cdn/2.m3u8", source_type="HLS", headers=template)

    first.headers["X-Extra"] = "1"
    template["Referer"] = "https://changed.example/"

    assert second.headers == {"Referer": "https://a.example/"}


def test_a_source_without_headers_defaults_to_empty():
    source = MediaSource(url="https://cdn/1.m3u8", source_type="HLS")
    assert source.headers == {}
