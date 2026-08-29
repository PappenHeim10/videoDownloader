"""The single authoritative store for user preferences.

Only one preference exists today - where finished videos go - but it is read by
the GUI, the CLI and the bootstrap, so it needs exactly one owner. Nothing else
in the application is allowed to read or write the config file directly.

Stored as JSON under `AppPaths.config_file` rather than through QSettings: the
native Windows QSettings backend is the registry, which is neither inspectable
nor backupable by the user, and the CLI would otherwise have to import a GUI
toolkit to read a single string.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from video_downloader.infrastructure.paths import AppPaths

logger = logging.getLogger(__name__)

DOWNLOAD_DIRECTORY_KEY = "download_directory"


class AppSettings:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.default()

    # --- persistence -------------------------------------------------------

    def _read(self) -> dict:
        try:
            # utf-8-sig, not utf-8: this file lives where the user can reach it,
            # and every common Windows editor - Notepad, PowerShell's Set-Content,
            # VS Code with the BOM setting on - writes UTF-8 with a byte order
            # mark. Plain utf-8 chokes on it and the configured directory would be
            # silently discarded. Writing stays BOM-free.
            raw = self.paths.config_file.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError as error:
            logger.warning("Settings konnten nicht gelesen werden: %s", error)
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            # A corrupt file must not take the application down. Starting from
            # defaults means the user is asked for a directory again, which is
            # recoverable; crashing on startup is not.
            logger.warning("Settings sind beschaedigt, starte mit Standardwerten: %s", error)
            return {}

        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.paths.ensure_root()
        self.paths.config_file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # --- download directory ------------------------------------------------

    def get_download_directory(self) -> Path | None:
        """The configured directory, or None if unset or no longer usable.

        An entry pointing at something that has been deleted or replaced by a
        file is treated as absent, so the caller asks again instead of silently
        writing somewhere unexpected.
        """
        value = self._read().get(DOWNLOAD_DIRECTORY_KEY)
        if not isinstance(value, str) or not value:
            return None

        candidate = Path(value).expanduser()
        if not candidate.is_dir():
            logger.info("Konfigurierter Download-Ordner existiert nicht mehr: %s", candidate)
            return None
        return candidate

    def set_download_directory(self, directory: str | Path) -> Path:
        resolved = Path(directory).expanduser().resolve()
        data = self._read()
        data[DOWNLOAD_DIRECTORY_KEY] = str(resolved)
        self._write(data)
        logger.info("Download-Ordner gesetzt: %s", resolved)
        return resolved
