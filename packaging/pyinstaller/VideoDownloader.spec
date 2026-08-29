# PyInstaller spec for the production (onefile, windowed) build.
import os

from PyInstaller.utils.hooks import collect_submodules

# Anchored to this file rather than the working directory - see the Debug spec for
# why. This build was less exposed to the bug because its entry point lives inside
# the application package, so PyInstaller could walk the package from the script
# even with a pathex pointing somewhere else entirely.
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
SRC = os.path.join(REPO_ROOT, "src")


def _collect_runtime_submodules(package: str) -> list[str]:
    return [name for name in collect_submodules(package) if ".tests" not in name]


hiddenimports = (
    _collect_runtime_submodules("xhamster_api")
    + _collect_runtime_submodules("base_api")
    + _collect_runtime_submodules("video_downloader")
)

a = Analysis(
    [os.path.join(SRC, "video_downloader", "__main__.py")],
    pathex=[SRC],
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name="VideoDownloader", console=False)
