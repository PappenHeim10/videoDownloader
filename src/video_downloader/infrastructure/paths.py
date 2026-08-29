"""Where application-owned files live.

The one rule this module exists to enforce: the current working directory never
decides where anything lands. A packaged desktop application is started from
Explorer, a shortcut, a terminal or another drive, and all of those must resolve
to the same per-user location.

Deliberately free of Qt. The CLI shares these paths and has no reason to import
a GUI toolkit to find its log file, and the layers below the UI should not carry
UI-framework responsibilities. On Windows the result is the same directory
QStandardPaths.AppLocalDataLocation would produce.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "VideoDownloader"

#: Overrides the application root. Set by the test suite so no test ever reads or
#: writes the developer's real per-user directory.
HOME_ENV_VAR = "VIDEO_DOWNLOADER_HOME"


def _platform_root() -> Path:
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


@dataclass(frozen=True)
class AppPaths:
    """Per-user locations for configuration, logs and application state.

    Nothing here is derived from the working directory, the repository or the
    executable location.
    """

    root: Path

    @classmethod
    def default(cls) -> AppPaths:
        return cls(root=_platform_root())

    @property
    def config_file(self) -> Path:
        return self.root / "settings.json"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    def ensure_log_dir(self) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return self.log_dir

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root
