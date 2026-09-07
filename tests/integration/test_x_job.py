"""A job downloading the shape X publishes: finished files on a resolver transport.

X is the first provider that offers one complete file and still cannot be handed
to `BaseCore.download` directly, because its URLs come from the resolver rather
than from a transport the engine owns. That combination is what these tests are
about - not muxing, of which there is none here.

Two consequences of what X does *not* state shape the whole path, and both are
pinned below. It states no size, so the readability probe has no last byte to
ask for and the fast path onto our own transport never applies; and with no
size there is no estimated weight for the progress bar, which therefore has to
take its scale from the download itself.

The resolver is a double. What matters here is what it is asked for - which
page, which format - and what the job does with what comes back; a real yt-dlp
would answer the same questions more slowly and from the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from base_api.models import Media, MediaSource, MediaTrackInfo
from base_api.modules.errors import UnsupportedProtocolError

from video_downloader.application.download_service import (
    run_download_job,
    select_for_job,
    takes_single_source_path,
)
from video_downloader.application.provider_session import ProviderSession
from video_downloader.application.track_download import (
    YTDLP_TRANSPORT,
    TrackDownloadError,
    download_selection,
)
from video_downloader.application.track_selection import TrackSelection
from video_downloader.domain.download_job import DownloadJob, LifecycleState

POST_URL = "https://x.com/example_poster/status/2096518350553940450"
POST_ID = "2096518350553940450"

#: One body per tier, distinguishable so a test can say which one was fetched.
BODIES = {
    320: b"three-twenty" * 64,
    540: b"five-forty!!" * 128,
    720: b"seven-twenty" * 256,
    1080: b"ten-eighty!!" * 512,
}


def x_media() -> Media:
    """What the X adapter produces: four finished files, no roles, no sizes."""
    media = Media(provider="x", original_url=POST_URL, title="Example Poster - a post")
    media.sources = [
        MediaSource(
            url=f"https://video.x.test/{tier}.mp4",
            source_type=YTDLP_TRANSPORT,
            expected_size=None,
            quality_value=tier,
            quality_label=None,
            identity=f"x:{POST_ID}:http-{tier}",
            track=MediaTrackInfo(role=None, container="mp4", width=tier, height=tier),
        )
        for tier in sorted(BODIES)
    ]
    return media


class FakeYoutubeDL:
    """Stands in for the resolver, and records what it was asked for.

    Writes the body of whichever tier the format names, so a test can tell which
    file was fetched from the bytes on disk rather than from the double.
    """

    asked: list[tuple[tuple[str, ...], str]] = []

    def __init__(self, options: dict) -> None:
        self._options = options

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def download(self, urls: list[str]) -> None:
        selector = self._options["format"]
        FakeYoutubeDL.asked.append((tuple(urls), selector))
        body = BODIES[int(selector.rsplit("-", 1)[-1])]
        hook = (self._options.get("progress_hooks") or [None])[0]
        if hook is not None:
            # yt-dlp reports the real total once the response headers arrive;
            # nobody could state it beforehand.
            hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": len(body)})
            hook({
                "status": "downloading",
                "downloaded_bytes": len(body),
                "total_bytes": len(body),
            })
        Path(self._options["outtmpl"]).write_bytes(body)


@pytest.fixture
def resolver(monkeypatch):
    import yt_dlp

    FakeYoutubeDL.asked = []
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def session_for(media: Media) -> ProviderSession:
    class Registry:
        async def resolve(self, url: str) -> Media:
            return media

        async def close(self) -> None:
            return None

    class NoCore:
        """The engine must not be reached at all on this path."""

        async def download(self, configuration: object) -> bool:
            raise AssertionError("a resolver transport must not reach BaseCore")

        async def close(self) -> None:
            return None

    return ProviderSession(registry=Registry(), core=NoCore())


# --- routing ----------------------------------------------------------------


def test_a_resolver_transport_is_not_handed_to_the_engine_directly():
    """`BaseCore.download` has no transport for it, so it must not be offered one."""
    selection = select_for_job(x_media(), "best")

    assert selection.combined is not None
    assert selection.needs_muxing is False
    assert takes_single_source_path(selection) is False


def test_a_finished_file_on_a_resolver_transport_is_still_selectable():
    """The regression this guards: `select_source` knows only the engine's own
    transports, so an X media reaching it fails as an unsupported protocol even
    though every one of its files is downloadable."""
    selection = select_for_job(x_media(), 720)

    assert selection.combined.quality_value == 720


def test_an_unknown_transport_is_still_refused():
    """Widening the route for one transport must not open it for every string."""
    media = x_media()
    for source in media.sources:
        object.__setattr__(source, "source_type", "DASH")

    with pytest.raises(UnsupportedProtocolError):
        select_for_job(media, "best")


# --- the job ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_x_post_downloads_into_one_file(tmp_path, resolver):
    job = DownloadJob(url=POST_URL, quality="best", output_dir=tmp_path)

    await run_download_job(job, session_factory=lambda: session_for(x_media()))

    assert job.state == LifecycleState.COMPLETED, job.error
    assert job.output_file.suffix == ".mp4"
    assert job.output_file.read_bytes() == BODIES[1080]


@pytest.mark.asyncio
async def test_the_requested_tier_is_the_one_fetched(tmp_path, resolver):
    job = DownloadJob(url=POST_URL, quality=540, output_dir=tmp_path)

    await run_download_job(job, session_factory=lambda: session_for(x_media()))

    assert job.output_file.read_bytes() == BODIES[540]
    assert resolver.asked == [((POST_URL,), "http-540")]


@pytest.mark.asyncio
async def test_nothing_is_left_behind_in_the_work_directory(tmp_path, resolver):
    job = DownloadJob(url=POST_URL, quality="worst", output_dir=tmp_path)

    await run_download_job(job, session_factory=lambda: session_for(x_media()))

    assert job.state == LifecycleState.COMPLETED, job.error
    assert not (job.state_file.parent / job.id).exists()


@pytest.mark.asyncio
async def test_no_size_is_estimated_and_nothing_is_asked_about(tmp_path, resolver):
    """With no stated size there is no estimate, and no estimate is not "small".

    Asking would mean asking about a number nobody has - and the number X does
    publish is the one measured at up to 5.7x the truth, which is exactly why it
    is not used.
    """
    asked: list[int] = []
    job = DownloadJob(url=POST_URL, quality="best", output_dir=tmp_path)
    job.confirm_large_download = lambda _job, size: (asked.append(size), True)[1]

    await run_download_job(job, session_factory=lambda: session_for(x_media()))

    assert job.expected_bytes is None
    assert asked == []
    assert job.state == LifecycleState.COMPLETED, job.error


@pytest.mark.asyncio
async def test_the_progress_bar_takes_its_scale_from_the_download(tmp_path, resolver):
    """Nobody could weigh this phase in advance, so it adopts what it measures.

    Without that, the bar would report every step against a total of zero and
    never show a percentage at all.
    """
    seen: list[tuple[int, int]] = []
    job = DownloadJob(url=POST_URL, quality="best", output_dir=tmp_path)
    original = job.on_change

    def observe(changed: DownloadJob) -> None:
        if changed.total_segments:
            seen.append((changed.downloaded_segments, changed.total_segments))
        if original is not None:
            original(changed)

    job.on_change = observe

    await run_download_job(job, session_factory=lambda: session_for(x_media()))

    assert seen, "the download reported no progress at all"
    assert all(a[0] <= b[0] for a, b in zip(seen, seen[1:])), seen
    assert seen[-1] == (len(BODIES[1080]), len(BODIES[1080]))


# --- what the resolver is asked for -----------------------------------------


@pytest.mark.asyncio
async def test_the_resolver_is_asked_for_the_page_the_user_pasted(tmp_path, resolver):
    """The regression this guards is provider-specific knowledge in a shared layer.

    The resolver path re-resolves the page, because the URL in hand may have
    expired since the selection was made. Which page that is has to travel with
    the media: rebuilding it from the identity meant assuming one provider's
    link format, and an X post sent through that assumption came out as a
    YouTube watch URL with a post id where the video id belongs.
    """
    job = DownloadJob(url=POST_URL, quality="best", output_dir=tmp_path)

    await run_download_job(job, session_factory=lambda: session_for(x_media()))

    assert resolver.asked == [((POST_URL,), "http-1080")]


@pytest.mark.asyncio
async def test_a_merged_format_selector_is_never_requested(tmp_path):
    """Without ffmpeg, yt-dlp aborts a merge *and discards what it downloaded*."""
    source = MediaSource(
        url="https://video.x.test/1080.mp4",
        source_type=YTDLP_TRANSPORT,
        expected_size=None,
        identity=f"x:{POST_ID}:http-1080+hls-audio",
        track=MediaTrackInfo(role=None, container="mp4"),
    )

    with pytest.raises(TrackDownloadError) as failure:
        await download_selection(
            TrackSelection(combined=source),
            core=None,
            target=tmp_path / "post.mp4",
            work_dir=tmp_path / "work",
            page_url=POST_URL,
            stop_event=asyncio.Event(),
            report=lambda done, total: None,
        )

    # Reported as a failure of that track, which is what every other download
    # failure is: the caller learns which one, and a retry costs one download.
    assert isinstance(failure.value.cause, UnsupportedProtocolError)
