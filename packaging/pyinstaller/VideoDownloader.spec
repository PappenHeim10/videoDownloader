# PyInstaller spec for the production (onedir, windowed) build.
#
# A directory rather than one file, and the reason is measured. A onefile
# executable unpacks its whole payload into %TEMP% on every start, and this
# payload is 644 MB: Qt WebEngine alone is 342 MB (195 MB of
# Qt6WebEngineCore.dll, 102 MB of .pak resources, 54 MB of translations) and it
# drags 72 MB of Qt Quick and QML in with it. That is the login window - X only
# accepts a login on its own page - and it is not optional, so the unpacking is
# what had to go: several seconds of disk copying before the first window, every
# single launch, for a payload that never changes.
#
# The cost of onedir is that distribution is a folder rather than a file, which
# an installer or a zip answers. The debug build has always been onedir, so the
# two now differ only in console and entry point.
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
    # yt-dlp loads its extractors by name at run time, so nothing in the import
    # graph points at them and PyInstaller bundles none of them. Without this the
    # frozen build imports yt_dlp fine and then reports every YouTube and X URL
    # as unsupported - a failure that only shows up in the artifact, never in a
    # source run, which is why the smoke test asks yt-dlp's own registry for each
    # extractor this application resolves through.
    + _collect_runtime_submodules("yt_dlp")
)

a = Analysis(
    [os.path.join(SRC, "video_downloader", "__main__.py")],
    pathex=[SRC],
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="VideoDownloader", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="VideoDownloader")
