"""Resolving again after a login, and only then.

A provider that refuses a URL only because nobody is signed in says so with
`ProviderLoginRequired`, which carries the site, its login page and the cookies
that mean the login worked - and nothing about which provider raised it. This is
the file that pins what the download service does with that, because the two
ways to get it wrong are both bad: never retrying makes the login pointless, and
retrying more than once turns a refusal into a loop of windows.

Nothing here opens a window or touches a network. The registry is a double that
refuses the first time, and `request_login` is a coroutine a test controls, which
is exactly the shape the window supplies.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from base_api.models import Media, MediaSource

from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_refusal import ProviderLoginRequired
from video_downloader.application.provider_session import ProviderSession
from video_downloader.domain.download_job import DownloadJob, LifecycleState

POST_URL = "https://x.com/example_poster/status/2096518350553940450"


class _Refusal(ProviderLoginRequired):
    """What an adapter raises, reduced to the contract this layer reads."""

    site = "x.com"
    login_url = "https://x.com/login"
    required_cookies = ("auth_token", "ct0")


class _Core:
    def __init__(self) -> None:
        self.configurations: list = []

    async def download(self, configuration):
        self.configurations.append(configuration)
        return True

    async def close(self):
        pass


class _Registry:
    """Refuses `refusals` times for want of a login, then resolves."""

    def __init__(self, refusals: int) -> None:
        self._left = refusals
        self.resolved: list[str] = []

    async def resolve(self, url: str) -> Media:
        self.resolved.append(url)
        if self._left > 0:
            self._left -= 1
            raise _Refusal("X gibt diesen Beitrag ohne Anmeldung nicht heraus.")
        return Media(
            provider="x",
            original_url=url,
            title="A post",
            sources=[MediaSource(url="https://cdn.test/video.mp4", source_type="HTTP")],
        )

    async def close(self):
        pass


def session_for(refusals: int) -> tuple[ProviderSession, _Registry, _Core]:
    registry, core = _Registry(refusals), _Core()
    return ProviderSession(registry=registry, core=core), registry, core


def job_in(directory: Path, **kwargs) -> DownloadJob:
    return DownloadJob(url=POST_URL, quality="best", output_dir=directory, **kwargs)


@pytest.mark.asyncio
async def test_a_login_that_succeeds_buys_exactly_one_more_attempt(tmp_path):
    session, registry, core = session_for(refusals=1)
    asked: list = []

    async def sign_in(refusal):
        asked.append(refusal)
        return True

    job = job_in(tmp_path, request_login=sign_in)
    await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.COMPLETED
    assert registry.resolved == [POST_URL, POST_URL]
    assert len(core.configurations) == 1
    # The window is handed the refusal itself, so it knows which site to open
    # without the service having to translate anything.
    assert [(r.site, r.login_url, r.required_cookies) for r in asked] == [
        ("x.com", "https://x.com/login", ("auth_token", "ct0"))
    ]


@pytest.mark.asyncio
async def test_a_cancelled_login_leaves_the_refusal_as_it_was(tmp_path):
    """Closing the window is an answer about the login, not about the URL."""
    session, registry, core = session_for(refusals=1)

    async def decline(refusal):
        return False

    job = job_in(tmp_path, request_login=decline)
    await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.FAILED
    assert "_Refusal:" in (job.error or "")
    assert registry.resolved == [POST_URL]
    assert core.configurations == []


@pytest.mark.asyncio
async def test_with_nobody_to_ask_the_refusal_stands(tmp_path):
    """A CLI and a test have no window, and neither may block on one."""
    session, registry, _ = session_for(refusals=1)

    job = job_in(tmp_path)
    await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.FAILED
    assert registry.resolved == [POST_URL]


@pytest.mark.asyncio
async def test_a_refusal_that_survives_the_login_is_not_asked_again(tmp_path):
    """The guard against a loop of login windows over one hopeless URL."""
    session, registry, _ = session_for(refusals=2)
    asked: list = []

    async def sign_in(refusal):
        asked.append(refusal)
        return True

    job = job_in(tmp_path, request_login=sign_in)
    await run_download_job(job, session_factory=lambda: session)

    assert job.state == LifecycleState.FAILED
    assert len(asked) == 1
    assert registry.resolved == [POST_URL, POST_URL]


@pytest.mark.asyncio
async def test_a_refusal_after_a_login_is_still_logged_as_a_refusal(tmp_path, caplog):
    """It is a refusal all the way through, so it never grows a traceback."""
    session, _, _ = session_for(refusals=2)

    async def sign_in(refusal):
        return True

    job = job_in(tmp_path, request_login=sign_in)
    service_log = "video_downloader.application.download_service"
    with caplog.at_level(logging.DEBUG, logger=service_log):
        await run_download_job(job, session_factory=lambda: session)

    reported = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert [record.levelno for record in reported] == [logging.WARNING]
    assert reported[0].exc_info is None
