import os
import sys
import time
import shutil
import argparse
import subprocess
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
        
    duration = time.time() - start_time
    
    print_header("Build Summary")
    print(f"Build Mode      : {args.mode}")
    print(f"Tests           : {tests_passed}")
    print(f"Executable Build: Passed")
    print(f"Duration        : {duration:.2f} seconds")
    print(f"Output Location : {os.path.abspath(distpath)}")
    print("\nALL BUILDS AND TESTS PASSED SUCCESSFULLY")

if __name__ == "__main__":
    main()
