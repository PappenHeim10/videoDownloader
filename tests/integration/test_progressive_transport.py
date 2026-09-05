"""A progressive HTTP job, end to end, next to an HLS one.

`run_download_job` is driven for real: the registry hands back a `Media`, the
service chooses a transport, and a real `BaseCore` downloads it. The HLS side
records at the playlist/segment layer as the existing header tests do; the HTTP
side runs against a loopback server, because the progressive transport streams
its body and there is no fetch function to stand in for.

What is pinned here is what the *application* owns:

* the choice of transport, and that HLS keeps winning where it used to;
* that a failed HLS download never becomes a quietly different MP4;
* the unit a job's progress counters are in, and what the UI writes for it;
* that two jobs running at once share neither headers nor units.
"""

from __future__ import annotations

import asyncio
import random
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from base_api.base import BaseCore
from base_api.models import Media, MediaSource
from base_api.modules.config import DownloadConfigHLS, DownloadConfigHTTP, RuntimeConfig
from base_api.modules.errors import UnsupportedProtocolError

from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob, LifecycleState, ProgressUnit
from video_downloader.ui.main_window import progress_details

BLOB = random.Random(23).randbytes(8192)

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
    "media_360.m3u8\n"
)
MEDIA_PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    "#EXTINF:4.0,\nseg0.ts\n#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
)


# --- doubles ----------------------------------------------------------------


class OneMediaRegistry:
    def __init__(self, media: Media):
        self.media = media

    async def resolve(self, url: str) -> Media:
        return self.media

    async def close(self) -> None:
        return None


class RecordingCore:
    """A core stand-in: records every configuration it is handed."""

    def __init__(self, result=True, error: Exception | None = None):
        self.configurations: list = []
        self.result = result
        self.error = error

    async def download(self, configuration):
        self.configurations.append(configuration)
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self) -> None:
        return None


class ProgressCore:
    """A core stand-in that drives a job's progress callback and nothing else."""

    def __init__(self, steps: list[tuple[int, int]]):
        self.steps = steps
        self.configurations: list = []

    async def download(self, configuration):
        self.configurations.append(configuration)
        for done, total in self.steps:
            configuration.callback(done, total)
        return True

    async def close(self) -> None:
        return None


def hls_core() -> tuple[BaseCore, list[tuple[str, dict | None]]]:
    """A real BaseCore whose two network calls are replaced by recorders."""
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


class _MediaHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    recorded: list[dict] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        type(self).recorded.append({
            "path": self.path,
            "referer": self.headers.get("Referer"),
            "range": self.headers.get("Range"),
        })
        self.send_response(200)
        self.send_header("Content-Length", str(len(BLOB)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"blob-1"')
        self.end_headers()
        self.wfile.write(BLOB)

    def log_message(self, *args):
        pass


@contextmanager
def media_server():
    _MediaHandler.recorded = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MediaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _MediaHandler.recorded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def media_with(*sources: MediaSource, title: str = "A video") -> Media:
    return Media(
        provider="test",
        original_url="https://example.test/watch",
        title=title,
        sources=list(sources),
    )


def http_source(url: str, **kwargs) -> MediaSource:
    return MediaSource(url=url, source_type="HTTP", **kwargs)


def hls_source(url: str = "https://cdn.example/master.m3u8", **kwargs) -> MediaSource:
    return MediaSource(url=url, source_type="HLS", **kwargs)


async def run_job(media: Media, core, tmp_path: Path, **job_kwargs) -> DownloadJob:
    job = DownloadJob(
        url=media.original_url,
        quality=job_kwargs.pop("quality", "best"),
        output_dir=tmp_path,
        remux=job_kwargs.pop("remux", False),
        **job_kwargs,
    )
    session = ProviderSession(registry=OneMediaRegistry(media), core=core)
    await run_download_job(job, session_factory=lambda: session)
    return job


# --- which transport a job takes --------------------------------------------


@pytest.mark.asyncio
async def test_an_hls_media_still_takes_the_hls_configuration(tmp_path):
    core = RecordingCore()

    job = await run_job(media_with(hls_source()), core, tmp_path)

    assert job.state == LifecycleState.COMPLETED
    assert isinstance(core.configurations[0], DownloadConfigHLS)
    assert job.progress_unit is ProgressUnit.SEGMENTS


@pytest.mark.asyncio
async def test_a_media_with_no_playlist_takes_the_progressive_configuration(tmp_path):
    core = RecordingCore()
    source = http_source(
        "https://cdn.example/video.mp4", expected_size=382672246, quality_value=720,
        quality_label="720p",
    )

    job = await run_job(media_with(source), core, tmp_path)

    assert job.state == LifecycleState.COMPLETED
    config = core.configurations[0]
    assert isinstance(config, DownloadConfigHTTP)
    assert config.media_source is source
    # The provider's stated size travelled all the way to the transport.
    assert config.expected_size == 382672246
    assert config.state_path == str(job.state_file)
    assert config.path == str(job.output_file)
    assert job.progress_unit is ProgressUnit.BYTES


@pytest.mark.asyncio
async def test_hls_wins_when_a_media_offers_both(tmp_path):
    core = RecordingCore()
    playlist = hls_source()

    job = await run_job(
        media_with(http_source("https://cdn.example/1080.mp4", quality_value=1080), playlist),
        core,
        tmp_path,
        quality="1080p",
    )

    assert isinstance(core.configurations[0], DownloadConfigHLS)
    assert core.configurations[0].media_source is playlist
    assert job.progress_unit is ProgressUnit.SEGMENTS


@pytest.mark.asyncio
async def test_a_failed_hls_download_never_becomes_a_different_mp4(tmp_path):
    """No silent fallback: the transport failure is the answer."""
    core = RecordingCore(result=False)

    job = await run_job(
        media_with(hls_source(), http_source("https://cdn.example/1080.mp4", quality_value=1080)),
        core,
        tmp_path,
    )

    assert job.state == LifecycleState.FAILED
    # Exactly one attempt, on exactly one transport.
    assert len(core.configurations) == 1
    assert isinstance(core.configurations[0], DownloadConfigHLS)


@pytest.mark.asyncio
async def test_an_hls_transport_error_does_not_retry_on_the_progressive_file(tmp_path):
    core = RecordingCore(error=ConnectionError("the playlist host went away"))

    job = await run_job(
        media_with(hls_source(), http_source("https://cdn.example/1080.mp4", quality_value=1080)),
        core,
        tmp_path,
    )

    assert job.state == LifecycleState.FAILED
    assert "ConnectionError" in (job.error or "")
    assert len(core.configurations) == 1


@pytest.mark.asyncio
async def test_a_dash_only_media_is_still_an_unsupported_protocol(tmp_path):
    core = RecordingCore()

    job = await run_job(
        media_with(MediaSource(url="https://cdn.example/x.mpd", source_type="DASH")),
        core,
        tmp_path,
    )

    assert job.state == LifecycleState.FAILED
    assert UnsupportedProtocolError.__name__ in (job.error or "")
    assert core.configurations == []


@pytest.mark.asyncio
async def test_the_requested_quality_picks_among_progressive_files(tmp_path):
    core = RecordingCore()
    sources = [
        http_source("https://cdn.example/480.mp4", quality_value=480, quality_label="480p", expected_size=100),
        http_source("https://cdn.example/720.mp4", quality_value=720, quality_label="720p", expected_size=200),
        http_source("https://cdn.example/1080.mp4", quality_value=1080, quality_label="1080p", expected_size=400),
    ]

    job = await run_job(media_with(*sources), core, tmp_path, quality="720p")

    assert job.state == LifecycleState.COMPLETED
    assert core.configurations[0].media_source is sources[1]
    assert core.configurations[0].expected_size == 200


# --- progress ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_byte_progress_is_stored_and_labelled_as_bytes(tmp_path):
    core = ProgressCore([(0, 8_388_608), (4_194_304, 8_388_608), (8_388_608, 8_388_608)])

    job = await run_job(
        media_with(http_source("https://cdn.example/v.mp4", expected_size=8_388_608)),
        core,
        tmp_path,
    )

    assert job.progress_unit is ProgressUnit.BYTES
    assert job.downloaded_segments == 8_388_608
    assert job.total_segments == 8_388_608
    assert job.progress == 100.0
    assert progress_details(job) == "8.0 / 8.0 MiB"


@pytest.mark.asyncio
async def test_hls_progress_stays_segment_shaped_and_segment_worded(tmp_path):
    core = ProgressCore([(1, 12), (12, 12)])

    job = await run_job(media_with(hls_source()), core, tmp_path)

    assert job.progress_unit is ProgressUnit.SEGMENTS
    assert (job.downloaded_segments, job.total_segments) == (12, 12)
    assert progress_details(job) == "12 / 12 Segmente"


def test_an_unknown_total_is_neither_a_percentage_nor_zero_of_zero(tmp_path):
    """A server that states no length reports 0 for the whole run.

    Driven on the job directly: what the transport reports in that case is
    pinned in the engine's own suite, and what matters here is that the
    application does not turn a 0 into a claim.
    """
    job = DownloadJob(url="x", quality="best", output_dir=tmp_path)
    job.progress_unit = ProgressUnit.BYTES
    job.transition(LifecycleState.DOWNLOADING)
    job.update_progress(3_145_728, 0)

    assert job.has_known_total is False
    assert job.progress == 0.0
    assert progress_details(job) == "3.0 MiB"
    assert "0 / 0" not in progress_details(job)


@pytest.mark.asyncio
async def test_an_unknown_total_becomes_known_when_the_stream_ends(tmp_path):
    """The transport's last callback states the size that actually arrived."""
    core = ProgressCore([(0, 0), (1_048_576, 0), (3_145_728, 3_145_728)])

    job = await run_job(
        media_with(http_source("https://cdn.example/v.mp4")), core, tmp_path
    )

    assert job.state == LifecycleState.COMPLETED
    assert job.has_known_total is True
    assert progress_details(job) == "3.0 / 3.0 MiB"


def test_a_job_that_has_not_started_says_nothing_about_progress(tmp_path):
    job = DownloadJob(url="x", quality="best", output_dir=tmp_path)
    assert progress_details(job) == ""
    job.progress_unit = ProgressUnit.BYTES
    assert progress_details(job) == ""


# --- what the window shows --------------------------------------------------


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def item_for(job: DownloadJob):
    from unittest.mock import MagicMock

    from video_downloader.application.download_manager import DownloadManager
    from video_downloader.ui.main_window import DownloadItem

    return DownloadItem(job, MagicMock(spec=DownloadManager), lambda _job: None)


def test_a_running_download_of_unknown_size_gets_an_indeterminate_bar(qt_app, tmp_path):
    job = DownloadJob(url="x", quality="best", output_dir=tmp_path)
    job.progress_unit = ProgressUnit.BYTES
    job.state = LifecycleState.DOWNLOADING
    job.update_progress(3_145_728, 0)

    item = item_for(job)

    # Qt's own "running, end unknown" - not a bar parked at 0 %.
    assert (item.progress.minimum(), item.progress.maximum()) == (0, 0)
    assert "3.0 MiB" in item.status.text()


def test_the_bar_becomes_determinate_once_a_total_is_known(qt_app, tmp_path):
    job = DownloadJob(url="x", quality="best", output_dir=tmp_path)
    job.progress_unit = ProgressUnit.BYTES
    job.state = LifecycleState.DOWNLOADING
    job.update_progress(3_145_728, 0)
    item = item_for(job)

    job.update_progress(2_097_152, 4_194_304)
    item.refresh(job)

    assert (item.progress.minimum(), item.progress.maximum()) == (0, 100)
    assert item.progress.value() == 50
    assert "2.0 / 4.0 MiB" in item.status.text()


def test_a_queued_job_is_not_shown_as_busy(qt_app, tmp_path):
    """Only a running download with an unknown end earns the animation."""
    job = DownloadJob(url="x", quality="best", output_dir=tmp_path)

    item = item_for(job)

    assert (item.progress.minimum(), item.progress.maximum()) == (0, 100)
    assert item.progress.value() == 0


def test_an_hls_job_keeps_its_segment_wording_in_the_window(qt_app, tmp_path):
    job = DownloadJob(url="x", quality="best", output_dir=tmp_path)
    job.state = LifecycleState.DOWNLOADING
    job.update_progress(3, 12)

    item = item_for(job)

    assert "3 / 12 Segmente" in item.status.text()
    assert (item.progress.minimum(), item.progress.maximum()) == (0, 100)
    assert item.progress.value() == 25


# --- two jobs at once -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_parallel_hls_and_http_job_mix_neither_headers_nor_units(tmp_path):
    """One real HLS download and one real progressive download, concurrently.

    Each job gets its own session, which is how the application runs them, and
    each source carries its own transport headers. The pin is that neither
    crosses over - and that the two jobs end up counting different things.
    """
    referer_hls = {"Referer": "https://hls.example/"}
    referer_http = {"Referer": "https://http.example/"}

    playlist_core, playlist_requests = hls_core()
    progressive_core = BaseCore(RuntimeConfig())

    with media_server() as (base_url, http_requests):
        hls_media = media_with(
            hls_source("https://cdn.hls/master.m3u8", headers=dict(referer_hls)),
            title="A playlist video",
        )
        http_media = media_with(
            http_source(
                f"{base_url}/video.mp4",
                headers=dict(referer_http),
                expected_size=len(BLOB),
                quality_value=720,
                quality_label="720p",
            ),
            title="A progressive video",
        )

        try:
            hls_job, http_job = await asyncio.gather(
                run_job(hls_media, playlist_core, tmp_path),
                run_job(http_media, progressive_core, tmp_path),
            )
        finally:
            await playlist_core.close()
            await progressive_core.close()

    assert hls_job.state == LifecycleState.COMPLETED
    assert http_job.state == LifecycleState.COMPLETED

    # Each transport saw only its own Referer.
    assert all(headers == referer_hls for _, headers in playlist_requests)
    assert [entry["referer"] for entry in http_requests] == [referer_http["Referer"]]

    # Each job counts its own kind of thing.
    assert hls_job.progress_unit is ProgressUnit.SEGMENTS
    assert hls_job.total_segments == 2
    assert progress_details(hls_job).endswith("Segmente")

    assert http_job.progress_unit is ProgressUnit.BYTES
    assert http_job.total_segments == len(BLOB)
    assert progress_details(http_job).endswith("MiB")

    # And both actually produced their file.
    assert (tmp_path / "A playlist video.mp4").read_bytes() == b"SEGMENTBYTES" * 2
    assert (tmp_path / "A progressive video.mp4").read_bytes() == BLOB


@pytest.mark.asyncio
async def test_a_real_progressive_job_writes_the_file_and_clears_its_state(tmp_path):
    core = BaseCore(RuntimeConfig())

    with media_server() as (base_url, http_requests):
        try:
            job = await run_job(
                media_with(
                    http_source(f"{base_url}/video.mp4", expected_size=len(BLOB)),
                    title="Measured video",
                ),
                core,
                tmp_path,
            )
        finally:
            await core.close()

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file == tmp_path / "Measured video.mp4"
    assert job.output_file.read_bytes() == BLOB
    assert not (tmp_path / "Measured video.mp4.tmp").exists()
    assert not job.state_file.exists()
    # The transport asked once, for the whole file.
    assert [entry["range"] for entry in http_requests] == [None]
