import os
import sys
import time
import shutil
import argparse
import subprocess
import tempfile
from pathlib import Path

def print_header(title: str):
    print(f"\n{'='*50}\n{title}\n{'='*50}")

def run_tests() -> bool:
    print_header("Running Tests")
    # video_downloader, base_api and xhamster_api all come from the installed
    # distribution. Only the repo root is added, so that tests/integration can
    # import debug_main.py; deliberately NOT src/ or any vendored package path,
    # because that would let the tests pass without the install actually working.
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    
    # We use pytest
    cmd = [sys.executable, "-m", "pytest", "tests"]
    
    try:
        result = subprocess.run(cmd, env=env, check=True)
        return True
    except subprocess.CalledProcessError:
        print("\n[!] Tests failed! Aborting build.")
        return False

def clean_build():
    print_header("Cleaning Build Directories")
    build_dir = Path("build")
    if build_dir.exists():
        print("Removing build/")
        shutil.rmtree(build_dir, ignore_errors=True)
    print("Clean finished.")

def run_pyinstaller(spec_file: str, distpath: str, clean: bool):
    print_header(f"Running PyInstaller ({spec_file})")
    
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--workpath", "build",
        "--distpath", distpath
    ]
    if clean:
        cmd.append("--clean")
        
    cmd.append(spec_file)
    
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n[!] PyInstaller failed with exit code {e.returncode}.")
        return False

SMOKE_MARKER = "VideoDownloader smoke OK"

ARTIFACTS = {
    # mode -> path of the executable this mode's spec actually produces
    "dev": Path("dist") / "dev" / "VideoDownloader.Debug" / "VideoDownloader.Debug.exe",
    "release": Path("dist") / "release" / "VideoDownloader.exe",
}


def smoke_test_artifact(mode: str) -> bool:
    """Prove the executable we just built can start.

    A green build used to mean "PyInstaller exited 0", which says nothing about
    whether the artifact can import the application. It is also not enough to
    check the exit code of *some* executable: a stale binary from an earlier
    layout sat in dist/ for a day and failed with ModuleNotFoundError while the
    build reported success. So this runs the exact artifact of this mode, from an
    unrelated working directory, and requires the marker that only appears after
    video_downloader and bootstrap have been imported and the components built.
    """
    print_header("Smoke Testing Artifact")
    exe = ARTIFACTS[mode]
    if not exe.is_file():
        print(f"[!] Expected artifact not found: {exe}")
        return False

    print(f"Running {exe} --smoke-test")
    with tempfile.TemporaryDirectory() as foreign_cwd:
        result = subprocess.run(
            [str(exe.resolve()), "--smoke-test"],
            cwd=foreign_cwd,  # nothing may depend on being started from the repo
            capture_output=True,
            text=True,
            timeout=120,
        )
        leftovers = sorted(p.name for p in Path(foreign_cwd).iterdir())

    if result.returncode != 0:
        print(f"[!] Artifact exited with {result.returncode}.")
        print(result.stderr.strip()[-2000:])
        return False

    if SMOKE_MARKER not in result.stdout:
        print(f"[!] Artifact exited 0 but never printed {SMOKE_MARKER!r}.")
        print("    That means it did not reach the end of startup.")
        print(result.stdout.strip()[-2000:])
        return False

    if leftovers:
        print(f"[!] Artifact wrote into its working directory: {', '.join(leftovers)}")
        return False

    print("Artifact imported the application, started, and left the CWD untouched.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Reproducible Build Orchestrator")
    parser.add_argument("mode", choices=["dev", "release"], help="Build mode: 'dev' for fast directory builds, 'release' for clean single-file executables.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip the test suite.")
    parser.add_argument("--clean", action="store_true", help="Force a clean build by deleting caches and intermediate files.")
    
    args = parser.parse_args()
    
    start_time = time.time()
    
    print(f"Starting build in '{args.mode.upper()}' mode...")
    
    # Run tests unless skipped
    tests_passed = "Skipped"
    if not args.skip_tests:
        if not run_tests():
            sys.exit(1)
        tests_passed = "Passed"
    
    # Determine settings based on mode
    if args.mode == "dev":
        spec_file = r"packaging\pyinstaller\VideoDownloader.Debug.spec"
        distpath = r"dist\dev"
        is_clean = args.clean  # Default false for dev
    else:
        spec_file = r"packaging\pyinstaller\VideoDownloader.spec"
        distpath = r"dist\release"
        is_clean = True  # Always clean for release
    
    if is_clean:
        clean_build()
        
    build_success = run_pyinstaller(spec_file, distpath, clean=is_clean)
    if not build_success:
        sys.exit(1)

    if not smoke_test_artifact(args.mode):
        print("\n[!] Artifact smoke test failed. The build is not usable.")
        sys.exit(1)

    duration = time.time() - start_time
    
    print_header("Build Summary")
    print(f"Build Mode      : {args.mode}")
    print(f"Tests           : {tests_passed}")
    print(f"Executable Build: Passed")
    print(f"Artifact Smoke  : Passed ({ARTIFACTS[args.mode]})")
    print(f"Duration        : {duration:.2f} seconds")
    print(f"Output Location : {os.path.abspath(distpath)}")
    print("\nALL BUILDS AND TESTS PASSED SUCCESSFULLY")

if __name__ == "__main__":
    main()
