"""B8: the working directory must not decide where anything lives.

Videos, configuration and logs all used to be resolved relative to the process's
CWD, so the same executable wrote to different places depending on whether it was
started from Explorer, a shortcut or a terminal.

Every test here redirects the application root through VIDEO_DOWNLOADER_HOME, so
nothing ever touches the real per-user directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from video_downloader.infrastructure.paths import HOME_ENV_VAR, AppPaths
from video_downloader.infrastructure.settings import AppSettings


@pytest.fixture
def app_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "appdata"
    monkeypatch.setenv(HOME_ENV_VAR, str(root))
    return root


@pytest.fixture
def settings(app_root) -> AppSettings:
    return AppSettings()


# --- location independence --------------------------------------------------


def test_application_paths_do_not_depend_on_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    monkeypatch.chdir(a)
    from_a = AppPaths.default()
    monkeypatch.chdir(b)
    from_b = AppPaths.default()

    assert from_a.root == from_b.root
    assert from_a.config_file == from_b.config_file
    assert from_a.log_dir == from_b.log_dir


def test_configured_download_directory_survives_a_working_directory_change(tmp_path, app_root, monkeypatch):
    videos = tmp_path / "videos"
    videos.mkdir()
    AppSettings().set_download_directory(videos)

    monkeypatch.chdir(tmp_path / "videos")
    first = AppSettings().get_download_directory()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    second = AppSettings().get_download_directory()

    assert first == second == videos.resolve()


def test_paths_are_not_inside_the_repository(app_root):
    repo_root = Path(__file__).resolve().parents[2]
    paths = AppPaths.default()

    for candidate in (paths.root, paths.config_file, paths.log_dir):
        assert repo_root not in candidate.resolve().parents
        assert candidate.resolve() != repo_root


def test_the_real_default_root_is_a_per_user_location(monkeypatch):
    # Without the override we must still land somewhere per-user, never in CWD.
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    root = AppPaths.default().root
    assert root.is_absolute()
    assert root.name == "VideoDownloader"
    assert Path.cwd() not in root.parents


# --- first use --------------------------------------------------------------


def test_no_directory_is_configured_initially(settings):
    assert settings.get_download_directory() is None


def test_a_chosen_directory_is_persisted_and_returned(settings, tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()

    stored = settings.set_download_directory(videos)

    assert stored == videos.resolve()
    assert settings.get_download_directory() == videos.resolve()


def test_the_setting_survives_a_fresh_instance(settings, tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()
    settings.set_download_directory(videos)

    assert AppSettings().get_download_directory() == videos.resolve()


def test_settings_are_written_where_the_paths_helper_says(settings, tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()
    settings.set_download_directory(videos)

    config = AppPaths.default().config_file
    assert config.is_file()
    assert json.loads(config.read_text(encoding="utf-8"))["download_directory"] == str(videos.resolve())


# --- invalid stored values --------------------------------------------------


def test_a_directory_that_no_longer_exists_counts_as_unset(settings, tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()
    settings.set_download_directory(videos)
    videos.rmdir()

    assert settings.get_download_directory() is None


def test_a_path_that_is_now_a_file_counts_as_unset(settings, tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()
    settings.set_download_directory(videos)
    videos.rmdir()
    videos.write_text("not a directory")

    assert settings.get_download_directory() is None


def test_a_replacement_directory_overwrites_the_invalid_one(settings, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    settings.set_download_directory(gone)
    gone.rmdir()

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    settings.set_download_directory(replacement)

    assert settings.get_download_directory() == replacement.resolve()


def test_a_corrupt_settings_file_does_not_crash_the_application(settings, app_root):
    AppPaths.default().ensure_root()
    AppPaths.default().config_file.write_text("{ this is not json", encoding="utf-8")

    assert settings.get_download_directory() is None


def test_a_corrupt_settings_file_can_be_repaired_by_setting_a_directory(settings, tmp_path):
    AppPaths.default().ensure_root()
    AppPaths.default().config_file.write_text("garbage", encoding="utf-8")

    videos = tmp_path / "videos"
    videos.mkdir()
    settings.set_download_directory(videos)

    assert settings.get_download_directory() == videos.resolve()


# --- logging ----------------------------------------------------------------


def _captured_log_targets(monkeypatch) -> list[Path]:
    """Record which files logging is asked to write, without touching the root logger.

    Reconfiguring global logging from inside a test suite is both unreliable
    (basicConfig is a no-op once handlers exist) and rude to other tests.
    """
    import logging

    targets: list[Path] = []

    class _Recorder(logging.NullHandler):
        def __init__(self, filename, *args, **kwargs):
            super().__init__()
            targets.append(Path(filename))

    def _record_basic_config(*args, **kwargs):
        if kwargs.get("filename"):
            targets.append(Path(kwargs["filename"]))

    monkeypatch.setattr(logging, "FileHandler", _Recorder)
    monkeypatch.setattr(logging, "basicConfig", _record_basic_config)
    return targets


@pytest.mark.parametrize("debug", [False, True])
def test_gui_logging_resolves_through_the_application_log_directory(app_root, tmp_path, monkeypatch, debug):
    from video_downloader.bootstrap import configure_logging

    targets = _captured_log_targets(monkeypatch)
    monkeypatch.chdir(tmp_path)
    configure_logging(debug=debug)

    # CWD-independence itself is asserted by the dedicated test above; here the
    # point is only that the log target comes from AppPaths and not from a
    # relative "runtime/logs".
    assert targets, "no log file was requested"
    for target in targets:
        assert target.parent == AppPaths.default().log_dir.resolve()
        assert not target.is_relative_to(Path("runtime"))


def test_cli_logging_uses_the_same_log_directory(app_root, tmp_path, monkeypatch):
    from video_downloader.cli.console_app import configure_logging as cli_configure_logging

    targets = _captured_log_targets(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cli_configure_logging()

    assert targets
    for target in targets:
        assert target.parent == AppPaths.default().log_dir.resolve()


def test_logs_do_not_land_in_the_selected_video_directory(settings, tmp_path):
    videos = tmp_path / "videos"
    videos.mkdir()
    settings.set_download_directory(videos)

    log_dir = AppPaths.default().log_dir
    assert videos.resolve() not in log_dir.resolve().parents
    assert log_dir.resolve() != videos.resolve()
