"""What "signed in" means, decided without a browser in the room.

The rule used to live inside the Qt login window, next to the WebEngine profile
that produced the cookies. That put a decision about credentials in the one
module that cannot be tested without a GUI toolkit - and it is a decision worth
testing, because getting it wrong stores a session that does not work.

Three rules, and each one exists because of something a site actually does:

* **Only the required names are kept.** X sets `guest_id`, `gt`, `__cf_bm` and
  more on the way in. None of them is what "signed in" means to the resolver, so
  none of them is carried out.
* **The last value of a name wins.** A cookie can be reissued during the login,
  and the fresher value is the one the site expects back.
* **A pair is only complete while every name is present and non-empty.** A
  cookie withdrawn again means the login did not hold, and a half session is not
  one - the caller must never have to check.

What is deliberately *not* here: the waiting. X issues `auth_token` and a fresh
`ct0` in the same exchange, so a caller should let a moment pass after
`is_complete` first turns true and only then take the session. How long to wait,
and with what timer, belongs to whoever owns the event loop; this class only
answers what is true right now.

No Qt, no I/O, no timers - which is what makes the rule testable without driving
a real login.
"""

from __future__ import annotations

import logging
from typing import Iterable

from video_downloader.domain.site_session import SessionCookie, SiteSession

logger = logging.getLogger(__name__)


class LoginCollector:
    """Collects a site's session cookies and says when they add up to a session."""

    def __init__(self, site: str, required_cookies: Iterable[str]) -> None:
        self.site = site
        self.required: tuple[str, ...] = tuple(required_cookies)
        self._collected: dict[str, SessionCookie] = {}

    def note(self, name: str, value: str, domain: str) -> bool:
        """Record one cookie the page was given. Returns whether it was wanted.

        Overwrites by name on purpose: the last value is the current one.
        """
        if name not in self.required:
            return False
        self._collected[name] = SessionCookie(name=name, value=value, domain=domain)
        # The name only. A value here is an account.
        logger.debug("Login at %s: %s set", self.site, name)
        return True

    def forget(self, name: str) -> bool:
        """Record that a cookie was withdrawn. Returns whether one was held."""
        if self._collected.pop(name, None) is None:
            return False
        logger.debug("Login at %s: %s removed", self.site, name)
        return True

    @property
    def is_complete(self) -> bool:
        """Whether every required cookie is present with a non-empty value."""
        return all(
            name in self._collected and self._collected[name].value
            for name in self.required
        )

    @property
    def names(self) -> tuple[str, ...]:
        """The required names currently held, in the order they are required."""
        return tuple(name for name in self.required if name in self._collected)

    def session(self) -> SiteSession | None:
        """The session, or `None` while it is not complete.

        Built in the order the site required the names, not in the order they
        arrived, so two logins of the same site produce the same shape.
        """
        if not self.is_complete:
            return None
        return SiteSession.now(
            self.site, tuple(self._collected[name] for name in self.required)
        )
