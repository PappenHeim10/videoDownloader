"""Where finished videos go, and who decides it.

The rule is small and has always been right; what was wrong is where it lived.
It sat in `MainWindow`, which meant that a caller without a window - the console
downloader, the host serving a separate front end - either reimplemented it or
did without.

Three cases, and the middle one is the whole reason this is not just
`AppSettings.get_download_directory()`:

* A directory is configured and still exists. Use it.
* Nothing is configured, or what was configured has been deleted or replaced by
  a file. `AppSettings` reports both as absent, so the user is asked again -
  rather than inventing a fallback next to the executable or in the working
  directory, which is exactly how the destination used to depend on where the
  application happened to be started from.
* Nobody is there to ask. Then there is no directory, and the caller says so.
  That is an ordinary outcome and not a failure: the console downloader prints
  a usage error, and the window says the download was cancelled.

Asking is a port rather than a dependency. This layer knows that somebody has to
be asked; it does not know whether that is a folder picker, a message over a
socket, or a test handing back a path.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from video_downloader.infrastructure.settings import AppSettings

logger = logging.getLogger(__name__)

#: Show the user a folder chooser with this title, and answer with what they
#: picked - or `None` if they cancelled. Cancelling is a decision, not an error.
AskForDirectory = Callable[[str], "Path | None"]

FIRST_TIME_PROMPT = "Zielordner für Downloads wählen"
CHANGE_PROMPT = "Neuen Zielordner für Downloads wählen"


class DownloadDirectory:
    """The configured destination, and the rule for getting one when there is none."""

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings()

    @property
    def current(self) -> Path | None:
        """What is configured, or `None` if nothing usable is."""
        return self.settings.get_download_directory()

    def resolve(self, ask: AskForDirectory | None = None) -> Path | None:
        """The directory the next job should use, asking only if necessary.

        A directory chosen here is persisted immediately: the question is worth
        asking once, not once per download.
        """
        configured = self.current
        if configured is not None:
            return configured

        if ask is None:
            logger.info("No download directory configured and nobody to ask.")
            return None

        chosen = ask(FIRST_TIME_PROMPT)
        if chosen is None:
            return None
        return self.settings.set_download_directory(chosen)

    def change(self, ask: AskForDirectory) -> Path | None:
        """Ask for a new destination. `None` when the user cancelled.

        Future jobs only. Running and finished jobs keep the directory they were
        created with, so nothing moves under the user's feet.
        """
        chosen = ask(CHANGE_PROMPT)
        if chosen is None:
            return None
        return self.settings.set_download_directory(chosen)

    def set(self, directory: Path) -> Path:
        """Persist a directory somebody already chose, without asking.

        For a caller that did its own asking - a front end over a connection,
        which has shown its own dialog by the time it says anything.
        """
        return self.settings.set_download_directory(directory)
