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
