"""A1: every download goes URL -> ProviderRegistry -> Media -> BaseCore.

The application used to call `xhamster_api.Client.get_video()` directly, which
meant "the default website" was a fact baked into the download workflow. Now the
composition root registers providers and the workflow only knows a registry.

Two things are load-bearing here and are pinned separately:

* Provider *selection* runs through the real registry, so a raw `.m3u8` URL
  reaches the same job pipeline an xHamster URL does - there is no second HLS
  path in the application.
* Provider *resources* stay job-scoped. `XHamsterAdapter` owns a Client -> a
  BaseCore -> a curl session, and `ProviderRegistry.close()` closes it. Each job
  therefore builds and closes its own session, and one job's cancellation or
  failure must not touch another's.

Nothing here touches the network: the production registry is built for real, but
the one method that would fetch is stubbed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from base_api import DirectMediaAdapter, ProviderRegistry
from base_api.models import Media, MediaSource
from base_api.modules.errors import (
    AmbiguousProviderError,
    UnsupportedProtocolError,
    UnsupportedURLError,
)
from xhamster_api import XHamsterAdapter

from video_downloader.application.download_manager import DownloadManager
from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import (
    ProviderNotConfiguredError,
    ProviderSession,
)
from video_downloader.bootstrap import create_job_runner, create_provider_session
from video_downloader.domain.download_job import DownloadJob, LifecycleState

XHAMSTER_URL = "https://xhamster.com/videos/example-1"
DIRECT_URL = "https://cdn.example/live/master.m3u8"
UNSUPPORTED_URL = "https://example.com/watch?v=abc"


# --- doubles ----------------------------------------------------------------


def media_for(url: str, title: str = "A title", source_type: str = "HLS") -> Media:
    return Media(
        provider="fake",
        original_url=url,
        title=title,
        sources=[MediaSource(url="https://cdn.test/stream.m3u8", source_type=source_type)],
    )


class RecordingCore:
    """A BaseCore stand-in: records every DownloadConfigHLS it is handed."""

    def __init__(self, result=True, error: Exception | None = None, behaviour=None):
        self.configurations: list = []
        self.result = result
        self.error = error
        self.behaviour = behaviour
        self.close_calls = 0

    async def download(self, configuration):
        self.configurations.append(configuration)
        if self.error is not None:
            raise self.error
        if self.behaviour is not None:
            return await self.behaviour(configuration)
        return self.result

    async def close(self):
        self.close_calls += 1


class RecordingRegistry:
    def __init__(self, media: Media | None = None, error: Exception | None = None):
        self.media = media
        self.error = error
        self.resolved: list[str] = []
        self.close_calls = 0

    async def resolve(self, url: str) -> Media:
        self.resolved.append(url)
        if self.error is not None:
            raise self.error
        return self.media if self.media is not None else media_for(url)

    async def close(self) -> None:
        self.close_calls += 1


def session_factory(**kwargs):
    """A factory plus the list of sessions it produced, for lifetime assertions."""
    created: list[ProviderSession] = []

    def factory() -> ProviderSession:
        session = ProviderSession(
            registry=RecordingRegistry(
                media=kwargs.get("media"), error=kwargs.get("resolve_error")
            ),
            core=RecordingCore(
                result=kwargs.get("result", True),
                error=kwargs.get("download_error"),
                behaviour=kwargs.get("behaviour"),
            ),
        )
        created.append(session)
        return session

    return factory, created


def job_in(directory: Path, url: str = XHAMSTER_URL, **kwargs) -> DownloadJob:
    return DownloadJob(url=url, quality=kwargs.pop("quality", "best"), output_dir=directory, **kwargs)


async def _must_not_resolve(self, url):  # pragma: no cover - only runs on a bug
    raise AssertionError(f"{type(self).__name__} must not have been selected for {url}")


# --- provider selection through the real production registry ----------------


@pytest.mark.asyncio
async def test_an_xhamster_url_selects_the_xhamster_adapter(monkeypatch):
    session = create_provider_session()
    seen: list[tuple[str, str]] = []

    async def fake_resolve(self, url):
        seen.append((type(self).__name__, url))
        return media_for(url)

    monkeypatch.setattr(XHamsterAdapter, "resolve", fake_resolve)
    monkeypatch.setattr(DirectMediaAdapter, "resolve", _must_not_resolve)

    try:
        media = await session.registry.resolve(XHAMSTER_URL)
    finally:
        await session.close()

    assert seen == [("XHamsterAdapter", XHAMSTER_URL)]
    assert media.original_url == XHAMSTER_URL


@pytest.mark.asyncio
async def test_a_direct_m3u8_url_selects_the_direct_adapter(monkeypatch):
    # DirectMediaAdapter.resolve is network-free, so the real one runs here.
    monkeypatch.setattr(XHamsterAdapter, "resolve", _must_not_resolve)
    session = create_provider_session()

    try:
        media = await session.registry.resolve(DIRECT_URL)
    finally:
        await session.close()

    assert media.provider == "direct"
    assert media.title == "master"
    assert [(s.url, s.source_type) for s in media.sources] == [(DIRECT_URL, "HLS")]


@pytest.mark.asyncio
async def test_an_unsupported_url_keeps_the_registrys_own_error():
    session = create_provider_session()
    try:
        with pytest.raises(UnsupportedURLError):
            await session.registry.resolve(UNSUPPORTED_URL)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_the_production_registry_is_not_ambiguous_about_either_url(monkeypatch):
    # The two registered adapters must not both claim the same link; if they ever
    # do, resolve() raises AmbiguousProviderError instead of downloading.
    monkeypatch.setattr(XHamsterAdapter, "resolve", lambda self, url: media_for(url))
    session = create_provider_session()
    try:
        assert session.registry._providers  # composition actually happened
        for url in (XHAMSTER_URL, DIRECT_URL):
            claiming = [type(p).__name__ for p in session.registry._providers if p.supports(url)]
            assert len(claiming) == 1, f"{url} claimed by {claiming}"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_an_ambiguous_match_keeps_the_registrys_own_error(tmp_path):
    class GreedyProvider:
        def supports(self, url: str) -> bool:
            return True

        async def resolve(self, url: str) -> Media:  # pragma: no cover - never reached
            raise AssertionError("an ambiguous match must not resolve")

    registry = ProviderRegistry()
    registry.register(DirectMediaAdapter())
    registry.register(GreedyProvider())

    with pytest.raises(AmbiguousProviderError):
        await registry.resolve(DIRECT_URL)

    # ...and through the job layer it becomes a failed job that names the error.
    core = RecordingCore()
    job = job_in(tmp_path, DIRECT_URL)
    await run_download_job(
        job, session_factory=lambda: ProviderSession(registry=registry, core=core)
    )

    assert job.state == LifecycleState.FAILED
    assert "AmbiguousProviderError" in job.error
    assert core.configurations == []


# --- the resolved media reaching the existing download invocation -----------


@pytest.mark.asyncio
async def test_the_resolved_media_reaches_the_download_with_every_argument_preserved(tmp_path):
    media = media_for(XHAMSTER_URL, title="Why: does this work?")
    factory, created = session_factory(media=media)
    job = job_in(tmp_path, quality="720p", remux=False)

    await run_download_job(job, session_factory=factory)

    configuration = created[0].core.configurations[0]
    assert configuration.media_source is media.sources[0]
    assert configuration.quality == "720p"
    assert configuration.remux is False
    assert configuration.stop_event is job.stop_event
    assert configuration.segment_state_path == str(job.state_file)
    assert configuration.path == str(job.output_file)
    # The engine's own defaults for the parts A1 does not touch.
    assert configuration.segment_dir is None
    assert configuration.start_segment == 0
    assert configuration.cleanup_on_stop is True
    assert configuration.keep_segment_dir is False
    assert configuration.return_report is False

    # The callback is the coalescing one, still wired to this job's progress.
    configuration.callback(3, 4)
    assert (job.downloaded_segments, job.total_segments) == (3, 4)


@pytest.mark.asyncio
async def test_the_remux_default_still_applies_when_the_job_does_not_decide(tmp_path):
    factory, created = session_factory()
    job = job_in(tmp_path)
    job.remux = None  # type: ignore[assignment]

    await run_download_job(job, session_factory=factory, remux=True)

    assert created[0].core.configurations[0].remux is True


@pytest.mark.asyncio
async def test_a_successful_download_still_leaves_the_output_file_on_the_job(tmp_path):
    factory, _ = session_factory(media=media_for(XHAMSTER_URL, title="Why: does this work?"))
    job = job_in(tmp_path)

    await run_download_job(job, session_factory=factory)

    assert job.state == LifecycleState.COMPLETED
    assert job.output_file == tmp_path / "Why_ does this work_.mp4"
    assert job.title == "Why: does this work?"


@pytest.mark.asyncio
async def test_a_direct_hls_url_uses_the_same_pipeline_as_a_site_url(tmp_path):
    """Requirement 6: one job path, not a special case for raw playlists."""
    direct_media = await DirectMediaAdapter().resolve(DIRECT_URL)

    site_factory, site_sessions = session_factory(media=media_for(XHAMSTER_URL, title="Site"))
    direct_factory, direct_sessions = session_factory(media=direct_media)

    site_job = job_in(tmp_path, XHAMSTER_URL, quality="1080p")
    direct_job = job_in(tmp_path, DIRECT_URL, quality="1080p")

    await run_download_job(site_job, session_factory=site_factory)
    await run_download_job(direct_job, session_factory=direct_factory)

    site_config = site_sessions[0].core.configurations[0]
    direct_config = direct_sessions[0].core.configurations[0]

    assert site_job.state == LifecycleState.COMPLETED
    assert direct_job.state == LifecycleState.COMPLETED
    assert direct_job.output_file == tmp_path / "master.mp4"
    # Same shape of invocation; only what the media itself carries differs.
    for field in ("quality", "remux", "start_segment", "cleanup_on_stop", "keep_segment_dir"):
        assert getattr(site_config, field) == getattr(direct_config, field)
    assert direct_config.media_source.source_type == "HLS"


@pytest.mark.asyncio
async def test_a_media_without_an_hls_source_fails_as_an_unsupported_protocol(tmp_path):
    factory, created = session_factory(media=media_for(DIRECT_URL, source_type="DASH"))
    job = job_in(tmp_path, DIRECT_URL)

    await run_download_job(job, session_factory=factory)

    assert job.state == LifecycleState.FAILED
    assert UnsupportedProtocolError.__name__ in job.error
    assert created[0].core.configurations == []


# --- provider-selection failures --------------------------------------------


@pytest.mark.asyncio
async def test_a_resolution_failure_fails_the_job_without_downloading(tmp_path):
    factory, created = session_factory(
        resolve_error=UnsupportedURLError("No provider registered to support URL: x")
    )
    job = job_in(tmp_path, UNSUPPORTED_URL)

    await run_download_job(job, session_factory=factory)

    assert job.state == LifecycleState.FAILED
    assert job.error.startswith("UnsupportedURLError:")
    assert created[0].core.configurations == []
    assert job.output_file is None


@pytest.mark.asyncio
async def test_provider_selection_failures_stay_distinguishable_from_other_failures(tmp_path):
    """Requirement 7: the job layer may fail the job, but not blur the diagnosis."""
    cases = {
        UnsupportedURLError("nope"): "UnsupportedURLError",
        AmbiguousProviderError("two"): "AmbiguousProviderError",
        ConnectionError("no network"): "ConnectionError",
        OSError("disk full"): "OSError",
    }
    for error, expected in cases.items():
        factory, _ = session_factory(resolve_error=error)
        job = job_in(tmp_path, UNSUPPORTED_URL)
        await run_download_job(job, session_factory=factory)
        assert job.state == LifecycleState.FAILED
        assert job.error.startswith(f"{expected}:")


@pytest.mark.asyncio
async def test_a_job_without_a_configured_provider_says_so_and_downloads_nothing(tmp_path):
    job = job_in(tmp_path)

    await run_download_job(job)

    assert job.state == LifecycleState.FAILED
    assert ProviderNotConfiguredError.__name__ in job.error
    assert job.output_file is None


# --- resource ownership ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"resolve_error": UnsupportedURLError("nope")},
        {"resolve_error": ConnectionError("no network")},
        {"download_error": RuntimeError("segments gone")},
        {"result": False},
    ],
    ids=["success", "selection-failure", "resolve-failure", "download-failure", "incomplete"],
)
async def test_a_job_closes_its_provider_resources_exactly_once(tmp_path, kwargs):
    factory, created = session_factory(**kwargs)
    job = job_in(tmp_path)

    await run_download_job(job, session_factory=factory)

    assert len(created) == 1
    assert created[0].core.close_calls == 1
    assert created[0].registry.close_calls == 1


@pytest.mark.asyncio
async def test_closing_a_session_twice_still_closes_it_once(tmp_path):
    factory, created = session_factory()
    job = job_in(tmp_path)

    await run_download_job(job, session_factory=factory)
    await created[0].close()  # an outer cleanup reaching the same session

    assert created[0].core.close_calls == 1
    assert created[0].registry.close_calls == 1


@pytest.mark.asyncio
async def test_a_cancelled_job_releases_its_provider_resources(tmp_path):
    async def blocks_until_stopped(configuration):
        while not configuration.stop_event.is_set():
            await asyncio.sleep(0.01)
        return type("Result", (), {"status": "cancelled"})()

    factory, created = session_factory(behaviour=blocks_until_stopped)
    manager = DownloadManager(tmp_path, job_runner=create_job_runner(session_factory=factory))
    job = manager.add_download(XHAMSTER_URL)
    await asyncio.sleep(0.05)

    await manager.cancel_download(job)

    assert job.state == LifecycleState.CANCELLED
    assert created[0].core.close_calls == 1
    assert created[0].registry.close_calls == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_two_simultaneous_jobs_keep_independent_resources_and_state(tmp_path):
    """The per-job isolation the old one-Client-per-job design guaranteed."""
    started = asyncio.Event()

    async def blocks_until_stopped(configuration):
        started.set()
        while not configuration.stop_event.is_set():
            await asyncio.sleep(0.01)
        return type("Result", (), {"status": "cancelled"})()

    factory, created = session_factory(behaviour=blocks_until_stopped)
    manager = DownloadManager(tmp_path, job_runner=create_job_runner(session_factory=factory))

    first = manager.add_download(XHAMSTER_URL)
    second = manager.add_download(DIRECT_URL)
    await started.wait()
    await asyncio.sleep(0.05)

    # A session per job: no provider resource is shared between the two.
    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].core is not created[1].core
    assert created[0].registry is not created[1].registry

    # Independent cancellation and resume state.
    assert first.stop_event is not second.stop_event
    assert first.state_file != second.state_file

    await manager.cancel_download(first)

    assert first.state == LifecycleState.CANCELLED
    assert not second.stop_event.is_set()
    assert second.state == LifecycleState.DOWNLOADING
    # The finished job released its own resources and only its own.
    first_index = 0 if created[0].core.configurations[0].stop_event is first.stop_event else 1
    assert created[first_index].core.close_calls == 1
    assert created[1 - first_index].core.close_calls == 0

    await manager.shutdown()
    assert second.state == LifecycleState.CANCELLED
    assert created[1 - first_index].core.close_calls == 1


# --- the composition root ----------------------------------------------------


def test_the_production_registry_registers_both_adapters():
    session = create_provider_session()
    try:
        registered = {type(p).__name__ for p in session.registry._providers}
        assert registered == {"XHamsterAdapter", "DirectMediaAdapter"}
    finally:
        asyncio.run(session.close())


def test_the_download_core_is_provider_clean_and_ownership_is_split():
    """Two transports, two owners, each closed exactly once.

    The adapter owns the client it scrapes with (`ProviderRegistry.close()`
    closes it), and the session owns the download core. No provider holds the
    download core, so nothing can install extraction state - the xHamster
    `Referer`, site cookies - on the session the job downloads over; what a
    media request needs travels on `MediaSource.headers` instead.
    """
    session = create_provider_session()
    try:
        adapter = next(
            p for p in session.registry._providers if isinstance(p, XHamsterAdapter)
        )
        assert adapter._owns_client is True
        assert adapter.client.core is not session.core
        # The extraction session carries the legacy site headers; the download
        # core has never had a session opened, let alone one with provider state.
        extraction_headers = {k.lower() for k in adapter.client.core.session.headers}
        assert "referer" in extraction_headers
        assert session.core.session is None
    finally:
        asyncio.run(session.close())


@pytest.mark.asyncio
async def test_the_manager_never_learns_about_providers(tmp_path):
    """Requirement 2: the registry is bound into the runner, not into the manager."""
    factory, created = session_factory()
    runner = create_job_runner(session_factory=factory)

    manager = DownloadManager(tmp_path, job_runner=runner)
    job = manager.add_download(XHAMSTER_URL)
    await job.asyncio_task

    assert job.state == LifecycleState.COMPLETED
    assert created[0].registry.resolved == [XHAMSTER_URL]
    assert not hasattr(manager, "registry")
    await manager.shutdown()
