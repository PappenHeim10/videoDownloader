"""The login window: what it takes away, and when it decides it is done.

No login happens here - that needs a person, a password and X's own page - so
what is pinned is everything around it: which cookies leave the window, that a
rotated value wins over the one it replaced, that a withdrawn cookie is not a
session, and that nothing is written outside the application's own store.

The cookies are handed to the window the way Qt hands them over, through
`cookieAdded`, with a double that has the two methods the handler calls. That is
deliberate rather than lazy: driving the real signal would mean driving a real
login, and the decisions worth testing are all on this side of it.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QApplication

login_window = pytest.importorskip(
    "video_downloader.ui.login_window",
    reason="Qt WebEngine is not available in this environment",
)
SiteLoginWindow = login_window.SiteLoginWindow

REQUIRED = ("auth_token", "ct0")


@pytest.fixture
def qt_app():
    return QApplication.instance() or QApplication([])


class _Field:
    """A `QByteArray` as far as the handler is concerned."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def data(self) -> bytes:
        return self._raw


class _Cookie:
    """What `cookieAdded` carries."""

    def __init__(self, name: str, value: str, domain: str = ".x.com") -> None:
        self._name, self._value, self._domain = name, value, domain

    def name(self) -> _Field:
        return _Field(self._name.encode())

    def value(self) -> _Field:
        return _Field(self._value.encode())

    def domain(self) -> str:
        return self._domain


@pytest.fixture
def window(qt_app, monkeypatch):
    """A window on a blank page: this file never asks X for anything."""
    # Short enough that the settling period is a test rather than a wait, long
    # enough that a second cookie in the same exchange still lands inside it.
    monkeypatch.setattr(login_window, "_SETTLE_MS", 30)
    dialog = SiteLoginWindow(
        site="x.com", login_url="about:blank", required_cookies=REQUIRED
    )
    yield dialog
    dialog.close()
    dialog.deleteLater()


def settle(qt_app, dialog, timeout: float = 5.0) -> None:
    """Let Qt run until the window has decided, or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and dialog.session is None:
        qt_app.processEvents()
        time.sleep(0.01)
    qt_app.processEvents()


def test_the_window_writes_nothing_to_disk(window):
    """An off-the-record profile is the whole reason no cookie file appears."""
    assert window._profile.isOffTheRecord()


def test_one_cookie_of_a_pair_is_not_a_session(qt_app, window):
    window._note(_Cookie("ct0", "csrf-before-login"))
    settle(qt_app, window, timeout=0.3)

    assert window.session is None


def test_the_noise_a_site_sets_on_the_way_in_is_not_carried_out(qt_app, window):
    for name in ("guest_id", "gt", "__cf_bm", "personalization_id"):
        window._note(_Cookie(name, "irrelevant"))
    window._note(_Cookie("auth_token", "token"))
    window._note(_Cookie("ct0", "csrf"))
    settle(qt_app, window)

    assert window.session is not None
    assert window.session.names() == REQUIRED


def test_a_rotated_cookie_wins_over_the_one_it_replaced(qt_app, window):
    """X issues a fresh `ct0` with `auth_token`; the stale one would fail CSRF.

    This is what the settling period is for, and the assertion is the reason it
    exists: the value stored has to be the last one the site sent, not the first
    one that completed the pair.
    """
    window._note(_Cookie("ct0", "csrf-before-login"))
    window._note(_Cookie("auth_token", "token"))
    window._note(_Cookie("ct0", "csrf-after-login"))
    settle(qt_app, window)

    assert window.session is not None
    assert [cookie.value for cookie in window.session.cookies] == [
        "token", "csrf-after-login",
    ]
    assert window.result() == SiteLoginWindow.DialogCode.Accepted


def test_a_withdrawn_cookie_during_the_grace_period_is_not_a_session(qt_app, window):
    """A login that does not hold must not leave one behind."""
    window._note(_Cookie("auth_token", "token"))
    window._note(_Cookie("ct0", "csrf"))
    window._forget(_Cookie("auth_token", "token"))
    settle(qt_app, window, timeout=0.3)

    assert window.session is None


def test_closing_the_window_answers_with_no_session(qt_app, window):
    window.reject()
    qt_app.processEvents()

    assert window.session is None
    assert window.result() == SiteLoginWindow.DialogCode.Rejected
