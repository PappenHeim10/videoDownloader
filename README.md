# Video Downloader

A modern, asynchronous desktop application for downloading video from the sites listed below, featuring a responsive UI built with PySide6 and qasync. It handles segmented HLS streams, single-file progressive downloads, and separate video and audio tracks that have to be combined back into one file.

---

## 📥 How the Download Process Works

The download process is optimized for performance, resilience, and UI responsiveness. What a job actually does depends on what the provider offers, but every job passes the same five stages:

1. **Resolution:**
   The URL goes to whichever registered adapter claims it. The adapter queries or scrapes the site and returns a provider-neutral `Media` object — the title, the duration, and one `MediaSource` per downloadable variant. Each adapter owns the session it extracts with, so whatever that session picks up along the way — a `Referer`, a cookie the site sets during the page fetch — dies with it and never reaches the download engine.

2. **Track Selection:**
   You ask for a position — `best`, `worst`, `half` — or for a concrete tier like `1080p` or `720`, and the sources are ranked by the provider's own quality value. A provider that publishes finished files settles on exactly one source here. A provider that publishes picture and sound separately settles on two, and stage 5 combines them.

3. **Fetching:**
   Two paths, chosen per track by the source's own type rather than per website. An ordinary HTTP source goes through the engine's progressive transport, which owns resume, retries and atomic finalisation. A source that only a resolver can produce goes through that resolver — unless a single one-byte request, aimed at the last byte of the file, proves our own transport can read the whole thing. That probe guards against URLs that serve a fixed prefix and then refuse, which a download would otherwise discover as a failure in the middle, after transferring everything before it.

   For HLS sources this is where `BaseCore` earns its keep: instead of one massive file it fetches the many small `.ts` segments concurrently with `aiohttp`.

4. **State Tracking (Resume Capability):**
   Each track writes its progress to its own `.state.json` file while it downloads. If the app crashes, your PC reboots, or you pause or cancel, the download resumes later instead of starting from scratch — and a half-finished track is kept, so a cancelled two-track job does not cost twice as much to resume.

5. **Combining:**
   HLS segments are remuxed into a standard, widely-compatible `.mp4`, and the temporary segments are cleaned up. A separate video and audio track are muxed into one container losslessly — packet for packet, never re-encoded. A file that already arrived complete needs neither step. The finished file is moved onto its target exactly once, so an existing download is never replaced by a half-written one.

---

## 🎬 Supported Video Types

Three shapes of source, all ending in one playable file:

- **HLS (HTTP Live Streaming)** — video delivered in chunks through `.m3u8` playlists. The downloader parses the playlist, fetches the fragmented `.ts` transport streams concurrently, and compiles them into an `.mp4`.
- **Progressive files** — a single static file on a server, already carrying both picture and sound. Nothing to assemble: it is fetched with resume and retries, and it is done.
- **Separate video and audio tracks** — picture and sound published as two files, which is all YouTube offers. Both are fetched, then muxed together locally without re-encoding.

---

## 🌐 Accepted Sources

The architecture of this application is highly modular. The core downloading logic — HLS, concurrency, remuxing — is separated from the site-specific extraction logic. `create_provider_session()` in `bootstrap.py` is the single place that knows which sites are supported.

**Currently Supported Sources:**

- **xHamster** — via the `xhamster_api` package.
  - **Single Videos & Shorts:** download any individual video or short.
  - **Channels / Pornstars / Creators:** input a URL for a Channel, Pornstar or Creator, and the application orchestrates concurrent downloads for all of their videos and shorts.
- **PeerTube** — a watch URL on any instance. One `GET /api/v1/videos/{id}` call resolves it, which is the same request the official web player makes. An instance that has downloading disabled is reported as such rather than failing obscurely.
- **YouTube** — watch URLs, resolved through `yt-dlp`. YouTube publishes no combined format, so every download fetches a video track and an audio track and muxes them locally.
- **X (formerly Twitter)** — single posts on `x.com` and `twitter.com`, including the `/i/web/status/…` form and the `/status/…/video/1` link X produces when an attachment is opened directly. X hands out finished progressive MP4s, so there is nothing to assemble. A profile, a feed, or a post that carries no video is refused with a sentence that says which of those it was.
- **Direct media URLs** — an `.m3u8` or comparable technical media URL pasted straight in, with no site to scrape.

Live broadcasts and Spaces are refused rather than downloaded.

**Future Extensibility:**
Adding a site means writing one adapter that turns a URL into a `Media` with its list of `MediaSource` objects, and registering it in `create_provider_session()`. Selection, fetching, resume and muxing already work against that provider-neutral shape, and an adapter never touches the download engine's session — so a new site changes what can be downloaded without changing how downloading works.

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
