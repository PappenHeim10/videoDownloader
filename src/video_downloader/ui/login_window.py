"""The site's own login page, in a window, kept for nothing but its cookies.

Why the site's page and not a form of our own: X's login is a captcha, a
password, sometimes a two-factor code and sometimes an e-mail challenge, and
yt-dlp cannot log in to X at all - its Twitter extractor has no login path, and
`is_logged_in` is `bool(cookies['auth_token'])` and nothing else. A form here
would fail at the first challenge, and until it failed it would be holding a
password that is not ours to hold. So the real page is shown, and what this
application takes away is what a browser would have: two cookies.

Three decisions worth stating, because each one narrows what this window can
leak:

* **Off the record.** The profile is created without a name, which in Qt means
  nothing is written to disk - no cookie file, no cache, no history beside the
  application's own encrypted store. Verified on 2026-09-07: `isOffTheRecord()`
  answers True, and an anonymous visit to the login page reported its cookies
  through `cookieAdded` without leaving a profile directory behind.
* **Only the required names are kept.** X sets `guest_id`, `gt`, `__cf_bm` and
  more on the way in; none of them is what "signed in" means to the resolver,
  so none of them is carried out of this window.
* **Values are never logged.** Only names, only counts. A value here is an
  account.

The window closes itself. `auth_token` appears the moment the login succeeds,
and X rotates `ct0` in the same exchange, so completion waits a moment after
the first sight of a full pair and then takes the freshest values - closing on
the first byte would risk pairing a new token with the CSRF cookie from before
the login.

Imported lazily by the window that opens it: this is the only module that pulls
in Qt WebEngine, and a start that never signs in never loads it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout, QWidget

from video_downloader.application.login_completion import LoginCollector
from video_downloader.domain.site_session import SiteSession

logger = logging.getLogger(__name__)

#: How long to keep listening after the last required cookie first appears.
#: X issues `auth_token` and a fresh `ct0` in the same exchange, and the two
#: arrive as separate `cookieAdded` signals; closing between them would store a
#: CSRF cookie the session token has already outlived.
_SETTLE_MS = 2000


class SiteLoginWindow(QDialog):
    """Shows `login_url` until the site's session cookies exist.

    Ends in exactly one of two states: `session` holds every required cookie, or
    it is `None` because the user closed the window. There is no third answer -
    a half session is not one, and the caller must not have to check.
    """

    def __init__(
        self,
        *,
        site: str,
        login_url: str,
        required_cookies: Iterable[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.site = site
        # What "signed in" means is not a property of this window - it is a
        # property of the site, and it lives in the application layer where it
        # can be tested without a browser.
        self._collector = LoginCollector(site, required_cookies)
        self._required = self._collector.required
        self._settling: QTimer | None = None
        self.session: SiteSession | None = None

        self.setWindowTitle(f"Bei {site} anmelden")
        self.resize(520, 760)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Melde dich auf der Seite von {site} an. Das Fenster schliesst sich, "
            "sobald die Anmeldung erkannt wird."
        ))

        # The profile has no name, which is what makes it off the record: it
        # lives in memory and dies with this window. It is held as an attribute
        # because the page may not outlive it.
        self._profile = QWebEngineProfile(self)
        store = self._profile.cookieStore()
        store.cookieAdded.connect(self._note)
        store.cookieRemoved.connect(self._forget)

        self._view = QWebEngineView(self)
        self._view.setPage(QWebEnginePage(self._profile, self._view))
        layout.addWidget(self._view, 1)

        note = QLabel(
            "Gespeichert werden nur die Sitzungscookies "
            f"({', '.join(self._required)}) - verschluesselt fuer dieses "
            "Windows-Konto. Kein Passwort erreicht diese Anwendung."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)

        self._view.load(QUrl(login_url))

    # --- collecting --------------------------------------------------------

    def _note(self, cookie: object) -> None:
        """One cookie the page was given, handed to the collector as it arrived."""
        if self._collector.note(
            name=cookie.name().data().decode("utf-8", "replace"),
            value=cookie.value().data().decode("utf-8", "replace"),
            domain=str(cookie.domain()),
        ):
            self._begin_settling()

    def _forget(self, cookie: object) -> None:
        self._collector.forget(cookie.name().data().decode("utf-8", "replace"))

    def _begin_settling(self) -> None:
        """Start the grace period once, as soon as a full pair is in hand."""
        if self._settling is not None or not self._complete():
            return
        logger.info(
            "Anmeldung an %s erkannt (%s); warte kurz auf die endgueltigen Werte.",
            self.site, ", ".join(self._collector.names),
        )
        self._settling = QTimer(self)
        self._settling.setSingleShot(True)
        self._settling.timeout.connect(self._finish)
        self._settling.start(_SETTLE_MS)

    def _complete(self) -> bool:
        return self._collector.is_complete

    def _finish(self) -> None:
        """Accept with the freshest values, or keep waiting if one was revoked."""
        session = self._collector.session()
        if session is None:
            # A cookie was withdrawn while settling - the login did not hold.
            self._settling = None
            return
        self.session = session
        self.accept()


async def sign_in(
    *,
    site: str,
    login_url: str,
    required_cookies: Iterable[str],
    parent: QWidget | None = None,
) -> SiteSession | None:
    """Open the login window and await its answer, without blocking the loop.

    Shown rather than `exec()`ed on purpose: `exec` runs a nested Qt loop, and
    a login can take a minute of typing and waiting for a code. Downloads
    already running keep running, their progress keeps arriving, and a job that
    is cancelled while this is open cancels this too - the `finally` closes the
    window rather than leaving it orphaned on screen.
    """
    dialog = SiteLoginWindow(
        site=site, login_url=login_url, required_cookies=required_cookies, parent=parent
    )
    answer: asyncio.Future[SiteSession | None] = asyncio.get_running_loop().create_future()

    def settle(_code: int) -> None:
        if not answer.done():
            answer.set_result(dialog.session)

    dialog.finished.connect(settle)
    dialog.show()
    try:
        return await answer
    finally:
        dialog.close()
        dialog.deleteLater()
