"""Where a site session is kept between runs.

One file, `sessions.dat` next to `settings.json`, holding every site's session
as DPAPI-protected JSON. Deliberately not in `settings.json`: that file is the
user's to read and edit, is written in plain UTF-8 on purpose, and an account's
session token has no business in it.

Read once and cached, for a reason that is not performance: `load()` is called
by every resolution of a URL on that site, and each miss would be a file read
plus a DPAPI call on the thread doing the extraction.

The store still works where nothing can be protected at rest - a platform
without DPAPI, or a machine where the call fails - by keeping the session for
the run and saying so through `persists`. That is the honest shape: a session
that has to be established once per start is a nuisance, and a plaintext token
in a file that looks protected is a hazard.

A blob that cannot be unprotected is treated as no session at all. It means the
file came from another account or machine, and the answer is another login, not
an error the user cannot act on.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from video_downloader.domain.site_session import SessionCookie, SiteSession
from video_downloader.infrastructure import secret_store
from video_downloader.infrastructure.paths import AppPaths

logger = logging.getLogger(__name__)


class SessionStore:
    """Every site session this user has, protected at rest where possible."""

    def __init__(self, paths: AppPaths | None = None, secrets: Any = None) -> None:
        self.paths = paths or AppPaths.default()
        # Injectable so a test can drive a failing or absent secret store
        # without touching the platform's real one.
        self._secrets = secrets or secret_store
        self._cache: dict[str, SiteSession] | None = None

    @property
    def persists(self) -> bool:
        """Whether a session saved now will still be here after a restart."""
        return bool(self._secrets.available())

    # --- reading -----------------------------------------------------------

    def load(self, site: str) -> SiteSession | None:
        """The session for `site`, or `None` when there is none to use."""
        return self._sessions().get(site)

    def sites(self) -> tuple[str, ...]:
        return tuple(sorted(self._sessions()))

    def _sessions(self) -> dict[str, SiteSession]:
        if self._cache is None:
            self._cache = self._read()
        return self._cache

    def _read(self) -> dict[str, SiteSession]:
        try:
            blob = self.paths.session_file.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as error:
            logger.warning("Gespeicherte Anmeldungen nicht lesbar: %s", error)
            return {}

        try:
            payload = json.loads(self._secrets.unprotect(blob).decode("utf-8"))
        except secret_store.SecretStoreError as error:
            # Another account, another machine, or a truncated file. Never the
            # blob itself in the log.
            logger.warning(
                "Gespeicherte Anmeldungen gehoeren nicht zu diesem Konto: %s", error
            )
            return {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            logger.warning("Gespeicherte Anmeldungen sind beschaedigt: %s", error)
            return {}

        if not isinstance(payload, dict):
            return {}
        return {
            site: session
            for site, entry in payload.items()
            if isinstance(site, str)
            for session in (_session_from(site, entry),)
            if session is not None
        }

    # --- writing -----------------------------------------------------------

    def save(self, session: SiteSession) -> bool:
        """Keep `session` for `session.site`. Returns whether it will survive.

        Kept in memory either way: a store that cannot write is still the place
        this run's resolutions read from.
        """
        self._sessions()[session.site] = session
        return self._flush()

    def clear(self, site: str) -> bool:
        """Forget `site`, in memory and on disk. This is the sign-out."""
        removed = self._sessions().pop(site, None) is not None
        self._flush()
        return removed

    def _flush(self) -> bool:
        sessions = self._sessions()
        if not self.persists:
            logger.info(
                "Anmeldung gilt nur fuer diese Sitzung: kein geschuetzter Speicher "
                "auf dieser Plattform."
            )
            return False

        try:
            if not sessions:
                self.paths.session_file.unlink(missing_ok=True)
                return True
            self.paths.ensure_root()
            payload = json.dumps({
                site: {
                    "captured_at": session.captured_at.isoformat(),
                    "cookies": [
                        {"name": c.name, "value": c.value, "domain": c.domain}
                        for c in session.cookies
                    ],
                }
                for site, session in sessions.items()
            })
            self.paths.session_file.write_bytes(
                self._secrets.protect(payload.encode("utf-8"))
            )
        except (OSError, secret_store.SecretStoreError) as error:
            logger.warning("Anmeldung konnte nicht gespeichert werden: %s", error)
            return False
        return True


def _session_from(site: str, entry: Any) -> SiteSession | None:
    """One stored entry back into a session, or `None` if it is not one.

    Every field is checked rather than trusted: this file is protected against
    another account reading it, not against this one having written an older
    shape.
    """
    if not isinstance(entry, dict):
        return None

    cookies = tuple(
        SessionCookie(
            name=cookie["name"], value=cookie["value"], domain=cookie["domain"]
        )
        for cookie in entry.get("cookies") or []
        if isinstance(cookie, dict)
        and all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain"))
    )
    if not cookies:
        return None

    try:
        captured_at = datetime.fromisoformat(str(entry.get("captured_at")))
    except ValueError:
        captured_at = datetime.now(timezone.utc)

    return SiteSession(site=site, cookies=cookies, captured_at=captured_at)
