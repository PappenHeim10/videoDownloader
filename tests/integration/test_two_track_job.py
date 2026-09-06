"""A job that downloads two tracks and combines them into one file.

Driven end to end through `run_download_job` with a real engine against the
loopback server, because everything that can go wrong here is about ordering:
which file exists when, what the progress bar says between phases, and what
survives a stop.

The single-source path is exercised alongside on purpose. It carries every
provider that existed before this feature, and "unchanged" is a claim that has
to be tested rather than asserted.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

import av
import pytest
from base_api.base import BaseCore
from base_api.models import Media, MediaSource, MediaTrackInfo
from base_api.modules.config import RuntimeConfig

from video_downloader.application.download_service import run_download_job
from video_downloader.application.muxing import MuxError
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob, LifecycleState, ProgressUnit

WATCH_URL = "https://provider.test/watch?v=abc"


# --- a server that serves whatever bytes a test gave it ---------------------


@contextmanager
def serving(files: dict[str, bytes]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - the base class names it
            body = files.get(self.path)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start = 0
            range_header = self.headers.get("Range")
            if range_header:
                start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            payload = body[start:]
            self.send_response(206 if range_header else 200)
            self.send_header("Accept-Ranges", "bytes")
            if range_header:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}"
                )
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


# --- real tiny tracks ------------------------------------------------------


def encode_video(path: Path) -> bytes:
    output = av.open(str(path), mode="w", format="mp4")
    stream = output.add_stream("libx264", rate=10)
    stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
    try:
        for index in range(10):
            frame = av.VideoFrame(64, 48, "yuv420p")
            frame.pts = index
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    finally:
        output.close()
    return path.read_bytes()


def encode_audio(path: Path) -> bytes:
    output = av.open(str(path), mode="w", format="mp4")
    stream = output.add_stream("aac", rate=48000)
    try:
        for index in range(40):
            frame = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.sample_rate = 48000
            frame.pts = index * 1024
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    finally:
        output.close()
    return path.read_bytes()


@pytest.fixture
def tracks(tmp_path_factory) -> dict[str, bytes]:
    scratch = tmp_path_factory.mktemp("tracks")
    return {
        "/video.mp4": encode_video(scratch / "v.mp4"),
        "/audio.m4a": encode_audio(scratch / "a.m4a"),
    }


def media_with_tracks(base_url: str, sizes: dict[str, int]) -> Media:
    media = Media(provider="test", original_url=WATCH_URL, title="Two Track Video")
    media.sources = [
        MediaSource(
            url=f"{base_url}/video.mp4",
            source_type="HTTP",
            quality_value=1080,
            quality_label="1080p",
            expected_size=sizes["/video.mp4"],
            identity="test:abc:v",
            track=MediaTrackInfo(
                role="video", container="mp4", video_codec="avc1.640028", fps=10.0,
                width=64, height=48,
            ),
        ),
        MediaSource(
            url=f"{base_url}/audio.m4a",
            source_type="HTTP",
            expected_size=sizes["/audio.m4a"],
            identity="test:abc:a",
            track=MediaTrackInfo(
                role="audio", container="m4a", audio_codec="mp4a.40.2",
                bitrate_bps=128_000,
            ),
        ),
    ]
    return media


def session_for(media: Media) -> ProviderSession:
    settings = RuntimeConfig()
    settings.request_attempts = 2
    settings.request_retry_initial_delay = 0.01
    settings.timeout = 10

    class Registry:
        async def resolve(self, url: str) -> Media:
            return media

        async def close(self) -> None:
            return None

    return ProviderSession(registry=Registry(), core=BaseCore(settings))


def streams_of(path: Path) -> set[str]:
    container = av.open(str(path))
    try:
        return {stream.codec_context.name for stream in container.streams}
    finally:
        container.close()


# --- the happy path --------------------------------------------------------


@pytest.mark.asyncio
async def test_two_tracks_are_downloaded_and_become_one_playable_file(tracks, tmp_path):
    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

        await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.COMPLETED, job.error
    assert job.output_file.exists()
    assert job.output_file.suffix == ".mp4"
    assert streams_of(job.output_file) == {"h264", "aac"}


@pytest.mark.asyncio
async def test_the_job_passes_through_muxing_on_its_way_to_completed(tracks, tmp_path):
    seen: list[LifecycleState] = []

    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)
        job.on_change = lambda changed: seen.append(changed.state)

        await run_download_job(job, session_factory=lambda: session)

    assert LifecycleState.MUXING in seen
    assert seen.index(LifecycleState.DOWNLOADING) < seen.index(LifecycleState.MUXING)
    assert seen[-1] == LifecycleState.COMPLETED


@pytest.mark.asyncio
async def test_progress_is_one_bar_over_both_tracks_and_the_mux(tracks, tmp_path):
    seen: list[tuple[int, int]] = []

    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)
        job.on_change = lambda changed: seen.append(
            (changed.downloaded_segments, changed.total_segments)
        )

        await run_download_job(job, session_factory=lambda: session)

    progress = [(done, total) for done, total in seen if total]
    assert progress, "the job should report progress"
    assert all(a[0] <= b[0] for a, b in zip(progress, progress[1:])), "must not go back"
    assert all(done <= total for done, total in progress), "must not exceed the total"
    assert job.progress_unit is ProgressUnit.BYTES


@pytest.mark.asyncio
async def test_the_per_job_track_directory_is_cleaned_up(tracks, tmp_path):
    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

        await run_download_job(job, session_factory=lambda: session)

    assert not (job.state_file.parent / job.id).exists()


# --- failure ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_track_names_which_one_and_keeps_the_other(tracks, tmp_path):
    """The finished track is the expensive part; a retry must not re-fetch it."""
    with serving({"/video.mp4": tracks["/video.mp4"]}) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

        await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.FAILED
    assert "audio" in job.error
    work_dir = job.state_file.parent / job.id
    assert (work_dir / "video.mp4").exists(), "the finished track must survive"


@pytest.mark.asyncio
async def test_an_existing_file_survives_a_failed_job(tracks, tmp_path):
    (tmp_path / "Two Track Video.mp4").write_bytes(b"yesterday's download")

    with serving({"/video.mp4": tracks["/video.mp4"]}) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

        await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.FAILED
    assert (tmp_path / "Two Track Video.mp4").read_bytes() == b"yesterday's download"


@pytest.mark.asyncio
async def test_a_stop_before_the_first_track_finishes_cancels_the_job(tracks, tmp_path):
    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)
        job.stop_event.set()

        await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.CANCELLED
    assert not (tmp_path / "Two Track Video.mp4").exists()


# --- the size gate ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_large_download_asks_first_and_stops_when_refused(tracks, tmp_path):
    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        # Claim a size past the threshold without moving any bytes.
        media.sources[0].expected_size = 3 * 1024**3
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)
        asked: list[int] = []
        job.confirm_large_download = lambda _job, estimate: (asked.append(estimate), False)[1]

        await run_download_job(job, session_factory=lambda: session)

    assert asked and asked[0] > 2 * 1024**3
    assert job.state == LifecycleState.FAILED
    assert "GiB" in job.error
    assert not (tmp_path / "Two Track Video.mp4").exists()


@pytest.mark.asyncio
async def test_without_a_confirmer_a_large_download_proceeds(tracks, tmp_path, caplog):
    """A CLI has nobody to ask; refusing on its behalf would be worse."""
    with serving(tracks) as base_url:
        media = media_with_tracks(base_url, {k: len(v) for k, v in tracks.items()})
        media.sources[0].expected_size = 3 * 1024**3
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

        await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.COMPLETED, job.error


# --- the old path is still the old path ------------------------------------


@pytest.mark.asyncio
async def test_a_single_combined_source_still_takes_the_engine_path(tracks, tmp_path):
    """Every provider that predates this feature must be untouched by it."""
    with serving(tracks) as base_url:
        media = Media(provider="test", original_url=WATCH_URL, title="One File")
        media.sources = [
            MediaSource(
                url=f"{base_url}/video.mp4",
                source_type="HTTP",
                quality_value=1080,
                expected_size=len(tracks["/video.mp4"]),
                track=MediaTrackInfo(role="combined", container="mp4"),
            )
        ]
        session = session_for(media)
        job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

        await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.COMPLETED, job.error
    assert job.output_file.read_bytes() == tracks["/video.mp4"]
    assert not job.state_file.exists(), "the engine clears its own resume state"
