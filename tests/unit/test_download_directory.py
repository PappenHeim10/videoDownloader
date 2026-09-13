"""Where downloads land, decided without a window.

The rule used to sit in `MainWindow`, so testing it meant constructing a
`QApplication`. Nothing here does - which is the point: the console downloader
and the host need the same rule, and a rule that needs a GUI toolkit to be
checked is one that will quietly diverge between callers.

Every test runs against an isolated settings root, so none of them reads or
writes the developer's real AppData.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_downloader.application.download_directory import (
    CHANGE_PROMPT,
    FIRST_TIME_PROMPT,
    DownloadDirectory,
)
from video_downloader.infrastructure.paths import HOME_ENV_VAR
from video_downloader.infrastructure.settings import AppSettings


@pytest.fixture
def settings(tmp_path, monkeypatch) -> AppSettings:
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    return AppSettings()


@pytest.fixture
def videos(tmp_path) -> Path:
    directory = tmp_path / "videos"
    directory.mkdir()
    return directory


class Picker:
    """A folder chooser, reduced to what the port promises."""

    def __init__(self, answer: Path | None) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, title: str) -> Path | None:
        self.prompts.append(title)
        return self.answer


# --- resolving ---------------------------------------------------------------


def test_a_configured_directory_is_used_without_asking(settings, videos):
    settings.set_download_directory(videos)
    picker = Picker(None)

    assert DownloadDirectory(settings).resolve(picker) == videos.resolve()
    assert picker.prompts == [], "the user was asked despite a valid setting"


def test_the_first_time_asks_and_remembers(settings, videos):
    picker = Picker(videos)
    directory = DownloadDirectory(settings)

    assert directory.resolve(picker) == videos.resolve()
    assert picker.prompts == [FIRST_TIME_PROMPT]
    # Asked once, not once per download.
    assert directory.resolve(picker) == videos.resolve()
    assert len(picker.prompts) == 1


def test_cancelling_leaves_nothing_configured(settings):
    picker = Picker(None)

    assert DownloadDirectory(settings).resolve(picker) is None
    assert picker.prompts == [FIRST_TIME_PROMPT]
    assert settings.get_download_directory() is None, "a fallback was invented"


def test_a_vanished_directory_is_asked_about_again(settings, tmp_path, videos):
    gone = tmp_path / "gone"
    gone.mkdir()
    settings.set_download_directory(gone)
    gone.rmdir()
    picker = Picker(videos)

    assert DownloadDirectory(settings).resolve(picker) == videos.resolve()
    assert picker.prompts == [FIRST_TIME_PROMPT], "the dead setting was used silently"


def test_a_directory_replaced_by_a_file_counts_as_absent(settings, tmp_path, videos):
    """`AppSettings` reports both cases the same, and so must this."""
    taken = tmp_path / "taken"
    taken.mkdir()
    settings.set_download_directory(taken)
    taken.rmdir()
    taken.write_text("not a directory", encoding="utf-8")

    assert DownloadDirectory(settings).resolve(Picker(videos)) == videos.resolve()


# --- nobody to ask -----------------------------------------------------------


def test_without_an_asker_an_unset_directory_is_simply_absent(settings):
    """The console downloader has no dialog, and must not grow one."""
    assert DownloadDirectory(settings).resolve() is None


def test_without_an_asker_a_configured_directory_still_works(settings, videos):
    settings.set_download_directory(videos)

    assert DownloadDirectory(settings).resolve() == videos.resolve()


# --- changing ----------------------------------------------------------------


def test_changing_asks_with_its_own_wording(settings, videos, tmp_path):
    settings.set_download_directory(videos)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    picker = Picker(elsewhere)

    assert DownloadDirectory(settings).change(picker) == elsewhere.resolve()
    assert picker.prompts == [CHANGE_PROMPT]
    assert settings.get_download_directory() == elsewhere.resolve()


def test_cancelling_a_change_keeps_the_old_directory(settings, videos):
    settings.set_download_directory(videos)

    assert DownloadDirectory(settings).change(Picker(None)) is None
    assert settings.get_download_directory() == videos.resolve()


def test_a_caller_that_did_its_own_asking_just_sets(settings, videos):
    """A front end over a connection has shown its own dialog already."""
    directory = DownloadDirectory(settings)

    assert directory.set(videos) == videos.resolve()
    assert directory.current == videos.resolve()


def test_current_reports_what_is_configured(settings, videos):
    directory = DownloadDirectory(settings)
    assert directory.current is None

    settings.set_download_directory(videos)
    assert directory.current == videos.resolve()
