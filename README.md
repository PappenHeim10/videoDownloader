# Video Downloader

A modern, asynchronous desktop application for downloading and processing HTTP Live Streaming (HLS) videos, featuring a responsive UI built with PySide6 and qasync.

---

## 📥 How the Download Process Works

The download process is heavily optimized for performance, resilience, and UI responsiveness. It follows a structured lifecycle for each video:

1. **Metadata Extraction:** 
   When you input a URL, the app instantiates an isolated `Client`. This client scrapes the target webpage to extract the raw video metadata, including the title and the master playlist URL (an `.m3u8` file).
   
2. **HLS Playlist Resolution:** 
   The master `.m3u8` playlist is fetched and parsed. If you requested a specific quality (e.g., `1080p`), the app finds the corresponding media playlist for that resolution. The media playlist contains hundreds or thousands of tiny video chunks, known as `.ts` (Transport Stream) segments.

3. **Concurrent Segment Downloading:**
   Instead of downloading a single massive file, the underlying `BaseCore` engine utilizes `aiohttp` to download multiple `.ts` segments concurrently. This maximizes your bandwidth utilization.
   
4. **State Tracking (Resume Capability):**
   During the download, the app writes progress to a temporary `.state.json` file. If the app crashes, your PC reboots, or you pause/cancel the download, you can resume it later without starting from scratch.

5. **Remuxing (Stitching it Together):**
   Once all the `.ts` segments are fully downloaded, the app performs a "remuxing" operation. It quickly merges and converts the raw stream data into a standard, widely-compatible `.mp4` file, and then cleans up the temporary segments.

---

## 🎬 Supported Video Types

The application is explicitly designed to handle **HLS (HTTP Live Streaming)** feeds. 

Unlike direct MP4 downloads (where the video is a single static file on a server), HLS delivers video in chunks via playlists. The downloader excels at parsing these `.m3u8` playlists, downloading the fragmented `.ts` transport streams, and compiling them into a final **`.mp4`** video.

---

## 🌐 Accepted Sources

The architecture of this application is highly modular. The core downloading logic (handling HLS, concurrency, and remuxing) is separated from the site-specific scraping logic.

**Currently Supported Sources:**
- **xHamster:** Fully integrated via the `xhamster_api` package. It accepts standard video URLs from this platform.
  - **Single Videos & Shorts:** Download any individual video or short.
  - **Channels / Pornstars / Creators:** You can input a URL for a Channel, Pornstar, or Creator, and the application will orchestrate concurrent downloads for all of their videos and shorts!

**Future Extensibility:**
Because the app uses isolated API clients, adding support for new websites (like YouTube, Vimeo, or other streaming platforms) simply requires creating a new client adapter that can extract an `.m3u8` URL from the target site's HTML. The underlying download engine (`BaseCore`) handles the rest automatically.

---

## 🛠️ Development Commands

The project commands live in `[tool.poe.tasks]` in `pyproject.toml` — the closest
Python gets to npm scripts. They are shortcuts around the same modules and scripts
you would otherwise call by hand, so nothing works *only* through the task runner.

### One-time setup

Install the task runner once, globally, as a command-line tool:

```powershell
python -m pip install --user pipx     # if pipx is missing
python -m pipx ensurepath             # then open a new shell
pipx install poethepoet
```

pipx keeps `poe` in its own virtual environment, so it never mixes into this
project's dependencies — and it stays available for every other project too.
`poethepoet` is listed under the `dev` extra as well, so a contributor who would
rather not install anything globally gets it from `pip install -e ".[dev]"` and
runs it as `.\.venv\Scripts\poe.exe`.

### Running the tasks

`poe` takes the project directory as an argument, so nothing depends on where
your shell happens to stand — not even `.venv\Scripts`:

```powershell
$repo = "C:\path\to\videoDownloader"    # the only line to adapt

poe -C $repo                            # list every task with its description
poe -C $repo run                        # start the GUI from source
poe -C $repo debug                      # start the GUI with debug logging (debug_main.py)
poe -C $repo cli "<URL>" -q best        # console downloader, no Qt window
poe -C $repo smoke                      # prove the startup path without opening a window
poe -C $repo test                       # pytest over tests/
poe -C $repo build                      # debug build   -> dist\dev
poe -C $repo release                    # release build -> dist\release
```

Standing inside the repo, drop the `-C $repo` and it is just `poe test`.

Installing or refreshing the dependencies is the one command that still needs an
explicit interpreter, because it is pip — not poe — that has to land in the right
environment:

```powershell
& "$repo\.venv\Scripts\python.exe" -m pip install -e "$repo[dev]"
```

### Why it is spelled this way

* **A globally installed `poe` still runs everything inside this project's venv.**
  It looks for `.venv` next to the `pyproject.toml` it was pointed at and puts it
  in front of `PATH` for the task. That is why `run` reaches PySide6 although the
  pipx environment has never heard of it.
* **`-C $repo` also sets the working directory** for every task, so `build` finds
  `packaging\` and `test` finds `tests\` regardless of where you started. The flip
  side: a relative path passed as an *argument* (say `-o .\out`) resolves against
  the repo, not against your shell.
* **`pip` must be addressed through the venv's `python.exe`.** A bare `pip` or
  `python` resolves through `PATH` to the system interpreter even when the current
  directory *is* `.venv\Scripts` — PowerShell never runs an executable from the
  current directory without a leading `.\`. The `&` in front is PowerShell's call
  operator, needed because the path comes from a variable.
* **`"$repo[dev]"` keeps its brackets.** PowerShell's simple `$var` expansion does
  not read them as an index, so pip receives the path plus the extra.
