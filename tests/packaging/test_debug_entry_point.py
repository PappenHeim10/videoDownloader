"""The Debug entry point and its crash reporter.

A stale Debug executable failed with ModuleNotFoundError at debug_main.py line 2
while the build reported success, because the build verified nothing about the
artifact and the console vanished before anyone could read the traceback. These
tests cover the two halves of that: the entry point must import, and a failure to
import must leave a trace that outlives the console.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEBUG_EXE = REPO_ROOT / "dist" / "dev" / "VideoDownloader.Debug" / "VideoDownloader.Debug.exe"


# --- source environment -----------------------------------------------------


def test_the_application_package_imports():
    import video_downloader  # noqa: F401


def test_bootstrap_imports():
    import video_downloader.bootstrap  # noqa: F401


def test_the_debug_entry_point_imports_the_application():
    import debug_main

    assert hasattr(debug_main, "run_application")


def test_the_smoke_marker_is_shared_between_bootstrap_and_build():
    import build as build_script
    from video_downloader.bootstrap import SMOKE_MARKER

    # If these drift apart the build silently stops proving anything.
    assert build_script.SMOKE_MARKER == SMOKE_MARKER


# --- crash reporter ---------------------------------------------------------


def test_the_crash_log_lands_next_to_the_application_logs(tmp_path, monkeypatch):
    import debug_main
    from video_downloader.infrastructure.paths import HOME_ENV_VAR, AppPaths

    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))

    # debug_main deliberately re-implements this in miniature, because importing
    # AppPaths is what may have failed. This pins the two together.
    assert debug_main._crash_log_path().parent == AppPaths.default().log_dir


def test_a_startup_failure_is_persisted_and_echoed(tmp_path, monkeypatch, capsys):
    import debug_main
    from video_downloader.infrastructure.paths import HOME_ENV_VAR

    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    try:
        raise ModuleNotFoundError("No module named 'video_downloader'")
    except ModuleNotFoundError as error:
        debug_main.report_startup_failure(error)

    written = debug_main._crash_log_path().read_text(encoding="utf-8")
    assert "ModuleNotFoundError" in written
    assert "video_downloader" in written
    assert "ModuleNotFoundError" in capsys.readouterr().err


def test_an_unwritable_crash_log_does_not_replace_the_original_error(tmp_path, monkeypatch, capsys):
    import debug_main
    from video_downloader.infrastructure.paths import HOME_ENV_VAR

    # Point the log at a path that cannot be created, then make sure reporting a
    # crash does not itself crash and the traceback still reaches stderr.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(HOME_ENV_VAR, str(blocker / "nested"))

    debug_main.report_startup_failure(RuntimeError("original failure"))

    assert "original failure" in capsys.readouterr().err


# --- the frozen artifact ----------------------------------------------------

def _artifact_is_current() -> bool:
    """Only test the artifact if it actually reflects the current sources.

    A stale executable is what started all this, so these tests refuse to grade
    one: an old binary passing would be worse than no test at all. It also breaks
    the circularity of build.py running the suite before producing the artifact -
    during that run the binary is stale or absent, so this skips, and build.py's
    own smoke gate verifies the fresh one afterwards.
    """
    if not DEBUG_EXE.is_file():
        return False
    built = DEBUG_EXE.stat().st_mtime
    sources = [*(REPO_ROOT / "src").rglob("*.py"), REPO_ROOT / "debug_main.py"]
    return all(source.stat().st_mtime <= built for source in sources)


needs_artifact = pytest.mark.skipif(
    not _artifact_is_current(),
    reason="Debug artifact missing or older than the sources; run `python build.py dev`",
)


@needs_artifact
def test_the_frozen_debug_artifact_starts_from_an_unrelated_directory():
    from video_downloader.bootstrap import SMOKE_MARKER

    with tempfile.TemporaryDirectory() as foreign_cwd:
        result = subprocess.run(
            [str(DEBUG_EXE), "--smoke-test"],
            cwd=foreign_cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        leftovers = sorted(p.name for p in Path(foreign_cwd).iterdir())

    assert result.returncode == 0, result.stderr
    # Not just the exit code: the marker only prints after video_downloader and
    # bootstrap were imported and the components constructed.
    assert SMOKE_MARKER in result.stdout
    assert "ModuleNotFoundError" not in result.stderr
    assert leftovers == []


@needs_artifact
def test_the_frozen_artifact_does_not_need_the_source_tree():
    # The bundled package must come out of the executable, not out of src/.
    internal = DEBUG_EXE.parent / "_internal"
    assert internal.is_dir()
    assert not (internal / "src").exists()
