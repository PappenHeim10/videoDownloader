# PyInstaller spec for the Debug (onedir, console) build.
import os

from PyInstaller.utils.hooks import collect_submodules

# Anchored to this file, not to the working directory. PyInstaller resolves script
# paths relative to the spec but pathex relative to the CWD, so the previous
# "../../src" pointed at a stranger's directory next to the repository and the
# application package was never on the analysis path. It only got bundled at all
# because it happened to be installed in the build environment.
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
SRC = os.path.join(REPO_ROOT, "src")


def _collect_runtime_submodules(package: str) -> list[str]:
    return [name for name in collect_submodules(package) if ".tests" not in name]


hiddenimports = (
    _collect_runtime_submodules("xhamster_api")
    + _collect_runtime_submodules("base_api")
    + _collect_runtime_submodules("video_downloader")
    # yt-dlp loads its extractors by name at run time, so nothing in the import
    # graph points at them and PyInstaller bundles none of them. Without this the
    # frozen build imports yt_dlp fine and then reports every YouTube URL as
    # unsupported - a failure that only shows up in the artifact, never in a
    # source run, which is why the smoke test below imports the extractor itself.
    + _collect_runtime_submodules("yt_dlp")
)

# The entry point sits outside the application package - it only imports it - so
# unlike the production build there is no script path for PyInstaller to walk the
# package from. That is exactly why pathex has to be right here.
a = Analysis(
    [os.path.join(REPO_ROOT, "debug_main.py")],
    pathex=[SRC, REPO_ROOT],
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="VideoDownloader.Debug", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="VideoDownloader.Debug")
