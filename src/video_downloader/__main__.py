import argparse
from video_downloader.bootstrap import run_application

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    args, _ = parser.parse_known_args()
    raise SystemExit(run_application(debug=False, smoke_test=args.smoke_test))

if __name__ == "__main__":
    main()
