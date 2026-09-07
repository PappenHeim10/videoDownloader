"""The yt-dlp configuration every call in this application starts from.

Split out of the YouTube adapter once a second provider and the download layer
needed the same options. Each entry below is a decision rather than a default,
and several of them are load-bearing for privacy - which is precisely why they
belong in one place instead of being restated per caller, where one omission
would be invisible.

The redacting logger is the other half of the same concern: yt-dlp reports what
it is doing, and what it is doing involves signed URLs.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class _RedactingLogger:
    """The logger handed to yt-dlp, because its own output is not safe to keep.

    Measured: with `verbose` on, yt-dlp emits the complete signed media URL -
    expiry, viewer IP, session id and signature. `verbose` is never passed, but
    a warning or an error can carry a URL too, and the debug channel is where a
    future release may put one. Every line is rewritten before it reaches the
    application log.
    """

    _URLISH = re.compile(r"https?://\S+")

    def _redact(self, message: object) -> str:
        def shorten(match: re.Match[str]) -> str:
            parts = urlsplit(match.group(0).rstrip('"\'.,;'))
            if not parts.query:
                return match.group(0)
            return f"{parts.scheme}://{parts.hostname}{parts.path}?<redacted>"

        return self._URLISH.sub(shorten, str(message))

    def debug(self, message: object) -> None:
        # yt-dlp routes its ordinary progress lines through debug as well.
        logger.debug("yt-dlp: %s", self._redact(message))

    def info(self, message: object) -> None:
        logger.debug("yt-dlp: %s", self._redact(message))

    def warning(self, message: object) -> None:
        logger.warning("yt-dlp: %s", self._redact(message))

    def error(self, message: object) -> None:
        logger.error("yt-dlp: %s", self._redact(message))


def install_session(resolver: Any, session: Any) -> None:
    """Put a site session into a resolver's cookie jar, in memory only.

    yt-dlp reads cookies from the jar it builds at construction, so a session
    can be installed after the fact and never has to become a file. Measured on
    2026-09-07 with a jar entry alone: `TwitterBaseIE.is_logged_in` - which is
    just `bool(cookies['auth_token'])` - answered True, and the extractor sent
    the `x-csrf-token` it derives from `ct0`. A cookie file would have been a
    plaintext account token on disk for the length of a resolution.

    Domain and path come from the site, not from here: what a cookie is valid
    for is the site's statement, and the jar matches `api.x.com` against a
    `.x.com` cookie by that rule alone. `secure` is set because these are
    session credentials and there is no https-less X to send them to.

    Only the names are logged. A value here is an account.
    """
    from http.cookiejar import Cookie

    for cookie in session.cookies:
        resolver.cookiejar.set_cookie(Cookie(
            version=0,
            name=cookie.name,
            value=cookie.value,
            port=None,
            port_specified=False,
            domain=cookie.domain,
            domain_specified=cookie.domain.startswith("."),
            domain_initial_dot=cookie.domain.startswith("."),
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
        ))
    logger.debug(
        "Session fuer %s installiert: %s", session.site, ", ".join(session.names())
    )


def base_options(**overrides: Any) -> dict[str, Any]:
    """The yt-dlp options every call in this application starts from.

    Written once, here, because each of these is a decision rather than a
    default and several of them are load-bearing for privacy:

    * `verbose=False` - non-negotiable, including in the debug build.
    * `cookiefile=None` and no cookie extraction - this application never reads
      a browser profile, and no session ever reaches the disk for yt-dlp to
      read. A site session established by the user in this application's own
      login window is installed straight into the resolver's cookie jar by
      `install_session`, so it exists in this process and nowhere else.
    * `cachedir=False` - nothing about a resolution is worth keeping on disk.
    * `postprocessors=[]` and `writeinfojson=False` - no ffmpeg step, no
      metadata sidecar next to the user's video.
    * an injected logger, so nothing yt-dlp says reaches a log unredacted.
    """
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "verbose": False,
        "cachedir": False,
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "postprocessors": [],
        "writeinfojson": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "logger": _RedactingLogger(),
    }
    options.update(overrides)
    return options
