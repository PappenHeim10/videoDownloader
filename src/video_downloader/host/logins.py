"""Logins a front end shows, and the decision of when they are finished.

The front end owns the browser; this module owns the rule. A front end that
shows a site's login page reports what cookies the page holds, over and over,
and the core decides when those add up to a session. That split is the one the
Qt window had internally - the page fed `LoginCollector`, a timer decided - and
it is kept across the connection so the rule stays in one place, testable
without a browser, instead of being rebuilt in a second language.

Why snapshots rather than one event per cookie: the WebView the Avalonia front
end uses has no cookie events on any platform, only a pull API. What the rule
needs is not events but observation finer than the settle period, and a
snapshot every 250 ms is eight observations inside it. A snapshot is also
simpler to reason about: a required name that is missing from one *is* a
withdrawn cookie, with no `cookieRemoved` to miss.

What "settle" means is unchanged from the window: once the required cookies are
first all present, wait `SETTLE_SECONDS` and then take the freshest values. X
issues `auth_token` and a fresh `ct0` in the same exchange, and closing between
the two would store a CSRF cookie the session token has already outlived. If a
cookie was withdrawn while waiting, the login did not hold, and the next
complete snapshot starts the wait again.

Values are never logged. Only names, only counts. A value here is an account.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from video_downloader.application.login_completion import LoginCollector
from video_downloader.domain.site_session import SiteSession

logger = logging.getLogger(__name__)

#: How long to keep watching after the required cookies are first all there.
#: The same 2,000 ms the Qt window waited, for the same reason.
SETTLE_SECONDS = 2.0


@dataclass
class _Login:
    id: str
    collector: LoginCollector
    settling: asyncio.TimerHandle | None = field(default=None)

    @property
    def site(self) -> str:
        return self.collector.site


class LoginRegistry:
    """The logins a front end is showing right now, by id."""

    def __init__(
        self,
        on_signed_in: Callable[[str, SiteSession], None],
        *,
        settle_seconds: float = SETTLE_SECONDS,
    ) -> None:
        # Called once per login, with the id and the finished session. Storing
        # it and telling the front end belong to the caller: this class only
        # knows when a login is done, not where a session lives.
        self._on_signed_in = on_signed_in
        self._settle_seconds = settle_seconds
        self._logins: dict[str, _Login] = {}

    @property
    def active_count(self) -> int:
        return len(self._logins)

    def begin(self, site: str, required_cookies: Iterable[str], login_id: str | None = None) -> str:
        """Start watching a login. Returns the id the front end reports under."""
        login = _Login(
            id=login_id or uuid.uuid4().hex,
            collector=LoginCollector(site, required_cookies),
        )
        self._logins[login.id] = login
        logger.info("Login at %s started (%s)", site, ", ".join(login.collector.required))
        return login.id

    def site_of(self, login_id: str) -> str | None:
        login = self._logins.get(login_id)
        return login.site if login else None

    def observe(self, login_id: str, cookies: Iterable[Any]) -> bool:
        """Take one snapshot of the page's cookies. Returns whether the login is open.

        A login that already finished or was cancelled answers False rather than
        failing: the front end polls on a timer, and one snapshot in flight
        while the login completes is a race it did not start.
        """
        login = self._logins.get(login_id)
        if login is None:
            return False

        # The last value of a name wins, inside one snapshot as across them - a
        # page can hold the same name for two domains while it rotates one.
        seen: dict[str, tuple[str, str]] = {}
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
            if isinstance(name, str) and name and isinstance(value, str) and isinstance(domain, str):
                seen[name] = (value, domain)

        collector = login.collector
        for name in collector.required:
            if name in seen:
                value, domain = seen[name]
                collector.note(name, value, domain)
            else:
                # Absent from a snapshot is what removed means here.
                collector.forget(name)

        if collector.is_complete and login.settling is None:
            logger.info(
                "Login at %s recognised (%s); waiting for the final values",
                login.site, ", ".join(collector.names),
            )
            login.settling = asyncio.get_running_loop().call_later(
                self._settle_seconds, self._settle, login.id
            )
        return True

    def discard(self, login_id: str) -> bool:
        """Stop watching a login without a session. Returns whether it was open."""
        login = self._logins.pop(login_id, None)
        if login is None:
            return False
        if login.settling is not None:
            login.settling.cancel()
        logger.info("Login at %s ended without a session", login.site)
        return True

    def discard_all(self, reason: str) -> None:
        """End every open login at once - the front end showing them is gone."""
        if not self._logins:
            return
        logger.info("Ending %d open login(s): %s", len(self._logins), reason)
        for login_id in list(self._logins):
            self.discard(login_id)

    def _settle(self, login_id: str) -> None:
        """Accept with the freshest values, or keep waiting if one was withdrawn."""
        login = self._logins.get(login_id)
        if login is None:
            return
        login.settling = None
        session = login.collector.session()
        if session is None:
            # A cookie was withdrawn while settling - the login did not hold.
            # The next complete snapshot starts the wait again.
            return
        del self._logins[login_id]
        self._on_signed_in(login_id, session)
