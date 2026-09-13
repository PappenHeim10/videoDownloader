"""What "signed in to a site" is, as a value.

A session here is the few cookies a site hands out when a person finishes its
own login page, and nothing else. No password ever becomes one of these: the
login happens on the site's own page in its own window, so what this application
ends up holding is the same thing a browser holds afterwards - a bearer token
with the lifetime the site chose.

That makes a `SiteSession` a credential, and the two rules that follow are the
reason it is its own type rather than a dict passed around:

* It is never logged. `__repr__` is written by hand to state the names and the
  count, never a value, because a dataclass repr would put an account's whole
  session into the first exception that carries it.
* It only travels to the site it belongs to. `site` is part of the value, and
  the resolver that gets these cookies is the one resolving a URL on that site.

No I/O, no Qt, no provider knowledge: this is the innermost layer, and the
window that captures a session and the store that persists it both speak in
these terms.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class SessionCookie:
    """One cookie, with the domain it was set for.

    The domain is carried rather than derived from `site`: a site sets its
    session on `.x.com` while the API that reads it lives on `api.x.com`, and
    matching those is the cookie jar's job, which it can only do from what the
    site actually said.
    """

    name: str
    value: str
    domain: str

    def __repr__(self) -> str:
        return (
            f"SessionCookie(name={self.name!r}, domain={self.domain!r}, "
            "value=<redacted>)"
        )


@dataclass(frozen=True)
class SiteSession:
    """The cookies one site handed out, and when.

    `captured_at` is kept because a session expires without saying so - X states
    no lifetime for `auth_token` - so the only honest thing the window can show
    is when it was established.
    """

    site: str
    cookies: tuple[SessionCookie, ...]
    captured_at: datetime

    @classmethod
    def now(cls, site: str, cookies: tuple[SessionCookie, ...]) -> SiteSession:
        return cls(site=site, cookies=cookies, captured_at=datetime.now(timezone.utc))

    def names(self) -> tuple[str, ...]:
        """The cookie names, which are safe to log; the values never are."""
        return tuple(cookie.name for cookie in self.cookies)

    def has_all(self, required: tuple[str, ...]) -> bool:
        """Whether every cookie a site needs to consider us signed in is here."""
        present = {cookie.name for cookie in self.cookies if cookie.value}
        return all(name in present for name in required)

    def __repr__(self) -> str:
        return (
            f"SiteSession(site={self.site!r}, cookies={self.names()!r}, "
            f"captured_at={self.captured_at.isoformat()}, values=<redacted>)"
        )
