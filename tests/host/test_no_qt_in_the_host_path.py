"""Nothing a headless caller imports may drag in a GUI toolkit.

This is the test that makes the split from `bootstrap` mean something. Without
it, one convenient import - a constant, a helper, a type - quietly puts PySide6
back on the path of the console downloader and of the host, and nobody notices
until a machine without Qt tries to run one of them.

Checked in a subprocess rather than in this one, because pytest has already
imported Qt for the window tests by the time this file runs: `sys.modules` here
would say Qt is loaded no matter what the host does.

`qasync` is checked alongside PySide6 for the same reason - it is the other half
of the window's event loop and has no business in a headless path either.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

#: Everything that must work without a window. The host modules, the
#: composition root they share with the window, and the console downloader -
#: which used to reach Qt through `bootstrap` and now does not.
HEADLESS_MODULES = [
    "video_downloader.composition",
    # Rules that used to live in a window and now do not. Listed here so the
    # move cannot be quietly undone by an import.
    "video_downloader.application.download_directory",
    "video_downloader.application.login_completion",
    "video_downloader.host.protocol",
    "video_downloader.host.asks",
    "video_downloader.host.events",
    "video_downloader.host.handlers",
    "video_downloader.host.server",
    "video_downloader.host.__main__",
    "video_downloader.cli.console_app",
]

FORBIDDEN = ("PySide6", "qasync", "shiboken6")


def _import_and_report(modules: list[str]) -> subprocess.CompletedProcess:
    script = textwrap.dedent(
        f"""
        import importlib, sys, json
        for name in {modules!r}:
            importlib.import_module(name)
        loaded = sorted(
            m for m in sys.modules
            if m.split(".")[0] in {FORBIDDEN!r}
        )
        print(json.dumps(loaded))
        """
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_headless_path_never_loads_qt():
    result = _import_and_report(HEADLESS_MODULES)

    assert result.returncode == 0, result.stderr
    loaded = result.stdout.strip().splitlines()[-1]
    assert loaded == "[]", (
        f"a headless import pulled in {loaded}. Something in the list above now "
        "imports the window's toolkit; move that import into the UI layer."
    )


def test_the_check_would_notice_a_qt_import():
    """The guard is only worth having if it can fail.

    Without this, a typo in the module list or a change in how `sys.modules` is
    read would leave a test that passes by not looking.
    """
    result = _import_and_report(["video_downloader.ui.main_window"])

    if result.returncode != 0:
        pytest.skip("Qt is not installed in this environment")
    assert "PySide6" in result.stdout, (
        "importing the window did not register PySide6 - the check above proves nothing"
    )
