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


def base_options(**overrides: Any) -> dict[str, Any]:
    """The yt-dlp options every call in this application starts from.

    Written once, here, because each of these is a decision rather than a
    default and several of them are load-bearing for privacy:

    * `verbose=False` - non-negotiable, including in the debug build.
    * `cookiefile=None` and no cookie extraction - this application never reads
      a browser profile and never sends a credential.
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
