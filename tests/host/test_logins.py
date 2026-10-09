"""When a login is finished, decided from snapshots of the page's cookies.

The Qt window received one event per cookie; the Avalonia front end can only
look. These tests pin the rule as it now runs in the core: the same three rules
`LoginCollector` already carries, plus the settle period that used to be a
`QTimer` in the window.
"""

from __future__ import annotations

import asyncio

import pytest

from video_downloader.host.logins import LoginRegistry

SETTLE = 0.05


def _cookie(name: str, value: str, domain: str = ".x.com") -> dict:
    return {"name": name, "value": value, "domain": domain}


class _Recorder:
    def __init__(self) -> None:
        self.sessions: list = []

    def __call__(self, login_id, session) -> None:
        self.sessions.append((login_id, session))


@pytest.mark.asyncio
async def test_a_complete_snapshot_settles_into_a_session():
    signed_in = _Recorder()
    logins = LoginRegistry(signed_in, settle_seconds=SETTLE)
    login_id = logins.begin("x.com", ("auth_token", "ct0"))

    logins.observe(login_id, [_cookie("auth_token", "t"), _cookie("ct0", "c")])
    assert signed_in.sessions == []  # not before the settle period

    await asyncio.sleep(SETTLE * 3)

    assert len(signed_in.sessions) == 1
    finished_id, session = signed_in.sessions[0]
    assert finished_id == login_id
    assert session.names() == ("auth_token", "ct0")
    assert logins.active_count == 0


@pytest.mark.asyncio
async def test_a_value_rotated_while_settling_is_the_one_kept():
    """The reason the settle period exists: `ct0` is reissued with the token."""
    signed_in = _Recorder()
    logins = LoginRegistry(signed_in, settle_seconds=SETTLE)
    login_id = logins.begin("x.com", ("auth_token", "ct0"))

    logins.observe(login_id, [_cookie("auth_token", "t"), _cookie("ct0", "before")])
    logins.observe(login_id, [_cookie("auth_token", "t"), _cookie("ct0", "after")])
    await asyncio.sleep(SETTLE * 3)

    session = signed_in.sessions[0][1]
    assert {cookie.name: cookie.value for cookie in session.cookies}["ct0"] == "after"


@pytest.mark.asyncio
async def test_a_cookie_withdrawn_while_settling_restarts_the_wait():
    signed_in = _Recorder()
    logins = LoginRegistry(signed_in, settle_seconds=SETTLE)
    login_id = logins.begin("x.com", ("auth_token", "ct0"))

    logins.observe(login_id, [_cookie("auth_token", "t"), _cookie("ct0", "c")])
    logins.observe(login_id, [_cookie("ct0", "c")])  # absent means removed
    await asyncio.sleep(SETTLE * 3)

    assert signed_in.sessions == []
    assert logins.active_count == 1

    logins.observe(login_id, [_cookie("auth_token", "t2"), _cookie("ct0", "c")])
    await asyncio.sleep(SETTLE * 3)

    assert len(signed_in.sessions) == 1


@pytest.mark.asyncio
async def test_an_empty_value_is_not_a_signed_in_cookie():
    signed_in = _Recorder()
    logins = LoginRegistry(signed_in, settle_seconds=SETTLE)
    login_id = logins.begin("x.com", ("auth_token", "ct0"))

    logins.observe(login_id, [_cookie("auth_token", ""), _cookie("ct0", "c")])
    await asyncio.sleep(SETTLE * 3)

    assert signed_in.sessions == []


@pytest.mark.asyncio
async def test_malformed_entries_are_skipped_rather_than_trusted():
    signed_in = _Recorder()
    logins = LoginRegistry(signed_in, settle_seconds=SETTLE)
    login_id = logins.begin("x.com", ("auth_token",))

    logins.observe(login_id, ["auth_token", {"name": "auth_token", "value": 7}, {}])
    await asyncio.sleep(SETTLE * 3)

    assert signed_in.sessions == []


@pytest.mark.asyncio
async def test_a_discarded_login_never_finishes():
    signed_in = _Recorder()
    logins = LoginRegistry(signed_in, settle_seconds=SETTLE)
    login_id = logins.begin("x.com", ("auth_token",))

    logins.observe(login_id, [_cookie("auth_token", "t")])
    assert logins.discard(login_id) is True
    await asyncio.sleep(SETTLE * 3)

    assert signed_in.sessions == []
    assert logins.observe(login_id, [_cookie("auth_token", "t")]) is False
