"""Manual, hard-bounded live probe against one real PeerTube video.

Outside `tests/` on purpose. It needs the network and it talks to a third
party, so a suite that ran it would fail for reasons that have nothing to do
with this code - and the alternative, a switch that makes the normal suite go
online, is a hidden production behaviour nobody would expect.

What it proves against the real instance rather than a fixture: the adapter
resolves the watch URL, the video really has no HLS playlist, a progressive
video source is chosen over the audio-only rendition, `fileUrl` is used and
`fileDownloadUrl` is not, the provider's stated size reaches the transport's
configuration, a bounded range request works, and the resume handshake asks for
the right offset and validates the `Content-Range` that comes back.

It fetches at most BUDGET bytes twice - about half a megabyte of a 382 MB file -
and deletes everything it wrote, whatever happens. Run it by hand from the
repository root:

    python tools/live_probe_peertube.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import base_api.base as engine
from base_api.base import BaseCore
from base_api.modules.config import DownloadConfigHTTP, RuntimeConfig
from base_api.modules.static_functions import load_progressive_state

from video_downloader.application.download_service import build_download_config, select_source
from video_downloader.infrastructure.event_loop import new_event_loop
from video_downloader.providers.peertube import PeerTubeAdapter

WATCH_URL = "https://video.blender.org/w/eJeLCkQyxvK1joGAaBf5PY"
#: 256 KiB per run. The file is ~382 MB; nothing here may fetch it.
BUDGET = 256 * 1024

checks: list[tuple[bool, str]] = []


def check(ok: bool, description: str, detail: str = "") -> None:
    checks.append((bool(ok), description))
    print(f"  [{'OK ' if ok else 'FAIL'}] {description}{f' -> {detail}' if detail else ''}")


async def main() -> int:
    print(f"Resolving {WATCH_URL}")
    adapter = PeerTubeAdapter()
    try:
        media = await adapter.resolve(WATCH_URL)
    finally:
        await adapter.close()

    kinds = [source.source_type for source in media.sources]
    check(bool(media.title), "the adapter resolved the watch URL", media.title)
    check("HLS" not in kinds, "the instance offers no HLS playlist", str(kinds))
    check(kinds and all(kind == "HTTP" for kind in kinds), "every source is progressive HTTP")

    source = select_source(media, "best")
    check(source.source_type == "HTTP", "a progressive HTTP source was selected")
    check(
        source.quality_value is not None and source.quality_value > 0,
        "the selected source is a video, not the audio-only rendition",
        f"quality_value={source.quality_value} label={source.quality_label!r}",
    )
    check("/object-storage/" in source.url, "fileUrl was used", source.url)
    check("/download/" not in source.url, "fileDownloadUrl was not used")
    check(source.headers == {}, "no headers were invented for the source")

    workdir = tempfile.mkdtemp(prefix="peertube-live-probe-")
    target = os.path.join(workdir, "probe.mp4")
    state_path = os.path.join(workdir, "probe.state.json")

    settings = RuntimeConfig()
    settings.request_attempts = 2
    core = BaseCore(settings)

    sent: list[dict] = []
    original_headers = BaseCore._progressive_headers

    def recording_headers(self, source_headers, offset, validators):
        headers = original_headers(self, source_headers, offset, validators)
        sent.append({
            key: value
            for key, value in headers.items()
            if key.lower() in {"range", "if-range", "accept-encoding"}
        })
        return headers

    parsed_ranges: list[tuple] = []
    original_parse = engine.parse_content_range

    def recording_parse(value):
        result = original_parse(value)
        parsed_ranges.append((value, result))
        return result

    BaseCore._progressive_headers = recording_headers
    engine.parse_content_range = recording_parse

    try:
        stop = asyncio.Event()

        def budget(written: int, total: int) -> None:
            if written >= BUDGET:
                stop.set()

        config = build_download_config(
            source,
            quality="best",
            path=target,
            callback=budget,
            stop_event=stop,
            state_path=state_path,
            remux=False,
        )
        check(
            isinstance(config, DownloadConfigHTTP),
            "the selection produced a DownloadConfigHTTP",
        )
        check(
            config.expected_size == source.expected_size and config.expected_size > 0,
            "the provider's stated size reached the HTTP configuration",
            f"{config.expected_size} bytes",
        )
        # Keep what the first run fetched, so the second run has something to
        # resume from. The default (delete on stop) is what the application uses.
        config.cleanup_on_stop = False
        config.chunk_size = 32 * 1024

        print(f"\nRun 1: fetching at most {BUDGET} bytes")
        first = await core.download(config)
        first_size = os.path.getsize(f"{target}.tmp")
        check(first is False, "the budget stop ended the run")
        check(0 < first_size < 4 * BUDGET, "a bounded prefix was written", f"{first_size} bytes")

        state = load_progressive_state(state_path)
        check(state is not None, "a resume state was written")
        check(state and state["kind"] == "http-progressive", "the state names its schema")
        check(state and state["version"] == 1, "the state names its version")
        check(
            state and state["total_size"] == source.expected_size,
            "the state recorded the resource's total size",
            str(state and state["total_size"]),
        )

        print(f"\nRun 2: resuming from byte {first_size}")
        stop.clear()
        resumed = asyncio.Event()

        def budget_again(written: int, total: int) -> None:
            if written >= first_size + BUDGET:
                resumed.set()

        config.callback = budget_again
        config.stop_event = resumed
        sent.clear()
        parsed_ranges.clear()

        second = await core.download(config)
        second_size = os.path.getsize(f"{target}.tmp")

        check(second is False, "the second budget stop ended the run")
        check(
            bool(sent) and sent[0].get("Range") == f"bytes={first_size}-",
            "the resume asked for exactly the next byte",
            str(sent[0] if sent else None),
        )
        check(
            all(entry.get("Accept-Encoding") == "identity" for entry in sent),
            "every request asked for an identity encoding",
        )
        satisfied = [(raw, out) for raw, out in parsed_ranges if out is not None]
        check(bool(satisfied), "the server answered 206 with a parsable Content-Range",
              str(satisfied[0][0]) if satisfied else "none")
        check(
            bool(satisfied) and satisfied[0][1][0] == first_size,
            "the Content-Range starts at the offset that was asked for",
        )
        check(
            bool(satisfied) and satisfied[0][1][2] == source.expected_size,
            "the Content-Range states the same total the provider did",
        )
        check(second_size > first_size, "the resume appended", f"{first_size} -> {second_size}")
        check(not os.path.exists(target), "no partial file was ever finalized")
    finally:
        BaseCore._progressive_headers = original_headers
        engine.parse_content_range = original_parse
        await core.close()
        for path in (target, f"{target}.tmp", state_path):
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass

    failed = [name for ok, name in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(), loop_factory=new_event_loop))
