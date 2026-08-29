from PyInstaller.utils.hooks import collect_submodules

def _collect_runtime_submodules(package: str) -> list[str]:
    return [name for name in collect_submodules(package) if ".tests" not in name]

hiddenimports = _collect_runtime_submodules("xhamster_api") + _collect_runtime_submodules("base_api") + _collect_runtime_submodules("video_downloader")
a = Analysis(["../../debug_main.py"], pathex=["../../src", "../.."], hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, exclude_binaries=True, name="VideoDownloader.Debug", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="VideoDownloader.Debug")
