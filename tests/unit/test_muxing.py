"""Combining two tracks into one file, without re-encoding either.

The fixtures are generated locally with PyAV rather than downloaded: the codec
pairs are what matters, and a two-second synthetic clip exercises the same muxer
paths a real one does at a thousandth of the size. Nothing here touches the
network.

The pairs are the ones measured against real tracks - H.264+AAC, VP9+Opus,
H.264+Opus, VP9+AAC - including the one combination that has to be refused, AAC
in WebM, which is the reason the Matroska fallback exists at all.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av
import pytest

from video_downloader.application.muxing import (
    ContainerChoice,
    MuxError,
    choose_container,
    mux_tracks,
)


# --- fixtures generated on the spot ----------------------------------------


def make_video(path: Path, codec: str, container: str, seconds: float = 1.0) -> Path:
    """A tiny silent clip. Small enough to be free, real enough to demux."""
    output = av.open(str(path), mode="w", format=container)
    stream = output.add_stream(codec, rate=10)
    stream.width, stream.height = 64, 48
    stream.pix_fmt = "yuv420p"
    stream.time_base = Fraction(1, 10)
    try:
        for index in range(int(seconds * 10)):
            frame = av.VideoFrame(64, 48, "yuv420p")
            frame.pts = index
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    finally:
        output.close()
    return path


def make_audio(path: Path, codec: str, container: str, seconds: float = 1.0) -> Path:
    output = av.open(str(path), mode="w", format=container)
    rate = 48000
    stream = output.add_stream(codec, rate=rate)
    try:
        samples = 1024
        for index in range(int(seconds * rate / samples)):
            frame = av.AudioFrame(format="fltp", layout="stereo", samples=samples)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.sample_rate = rate
            frame.pts = index * samples
            frame.time_base = Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
    finally:
        output.close()
    return path


@pytest.fixture
def h264(tmp_path: Path) -> Path:
    return make_video(tmp_path / "v.mp4", "libx264", "mp4")


@pytest.fixture
def vp9(tmp_path: Path) -> Path:
    return make_video(tmp_path / "v.webm", "libvpx-vp9", "webm")


@pytest.fixture
def aac(tmp_path: Path) -> Path:
    return make_audio(tmp_path / "a.m4a", "aac", "mp4")


@pytest.fixture
def opus(tmp_path: Path) -> Path:
    return make_audio(tmp_path / "a.webm", "libopus", "webm")


def streams_of(path: Path) -> tuple[set[str], str]:
    container = av.open(str(path))
    try:
        codecs = {stream.codec_context.name for stream in container.streams}
        return codecs, container.format.name
    finally:
        container.close()


# --- container choice ------------------------------------------------------


@pytest.mark.parametrize(
    ("video", "audio", "extension"),
    [
        ("avc1", "mp4a", ".mp4"),
        ("av01", "mp4a", ".mp4"),
        ("avc1", "opus", ".mp4"),
        ("vp09", "opus", ".webm"),
        ("vp09", "mp4a", ".mkv"),
        ("hev1", "vorbis", ".mkv"),
    ],
)
def test_the_container_follows_the_pair(video, audio, extension):
    assert choose_container(video, audio).extension == extension


def test_aac_never_lands_in_webm():
    """PyAV refuses it outright; the fallback exists so we never ask."""
    assert choose_container("vp09", "mp4a").format_name == "matroska"


def test_an_unknown_codec_falls_back_rather_than_guessing():
    assert choose_container("something-new", "mp4a").format_name == "matroska"


def test_a_silent_video_keeps_its_own_container():
    assert choose_container("vp09", None).extension == ".webm"
    assert choose_container("avc1", None).extension == ".mp4"


# --- muxing ----------------------------------------------------------------


def test_h264_and_aac_become_one_mp4(h264, aac, tmp_path):
    target = tmp_path / "out.mp4"

    mux_tracks(h264, aac, target, choose_container("avc1", "mp4a"))

    codecs, container = streams_of(target)
    assert codecs == {"h264", "aac"}
    assert "mp4" in container


def test_vp9_and_opus_become_one_webm(vp9, opus, tmp_path):
    target = tmp_path / "out.webm"

    mux_tracks(vp9, opus, target, choose_container("vp09", "opus"))

    codecs, container = streams_of(target)
    assert codecs == {"vp9", "opus"}
    assert "matroska" in container or "webm" in container


def test_a_mismatched_pair_lands_in_matroska(vp9, aac, tmp_path):
    target = tmp_path / "out.mkv"

    mux_tracks(vp9, aac, target, choose_container("vp09", "mp4a"))

    codecs, _ = streams_of(target)
    assert codecs == {"vp9", "aac"}


def test_a_video_without_audio_is_written_through(h264, tmp_path):
    target = tmp_path / "out.mp4"

    mux_tracks(h264, None, target, choose_container("avc1", None))

    codecs, _ = streams_of(target)
    assert codecs == {"h264"}


def test_nothing_is_re_encoded(h264, aac, tmp_path):
    """The output codecs must be the input codecs, not a transcode of them."""
    source_codecs = streams_of(h264)[0] | streams_of(aac)[0]

    mux_tracks(h264, aac, tmp_path / "out.mp4", choose_container("avc1", "mp4a"))

    assert streams_of(tmp_path / "out.mp4")[0] == source_codecs


def test_progress_is_monotonic_and_ends_at_the_total(h264, aac, tmp_path):
    seen: list[tuple[int, int]] = []

    mux_tracks(
        h264, aac, tmp_path / "out.mp4", choose_container("avc1", "mp4a"),
        callback=lambda done, total: seen.append((done, total)),
    )

    assert seen, "the mux should report progress"
    assert all(a[0] <= b[0] for a, b in zip(seen, seen[1:]))
    assert seen[-1][0] == seen[-1][1]


# --- failure ---------------------------------------------------------------


def test_a_failed_mux_leaves_the_inputs_untouched(h264, tmp_path):
    """The tracks are the expensive part; a retry must not cost a download."""
    not_audio = tmp_path / "broken.m4a"
    not_audio.write_bytes(b"not a media file at all")
    before = h264.read_bytes()

    with pytest.raises(MuxError):
        mux_tracks(h264, not_audio, tmp_path / "out.mp4", ContainerChoice("mp4", ".mp4"))

    assert h264.read_bytes() == before
    assert not_audio.exists()


def test_a_failed_mux_leaves_no_temporary_file_behind(h264, tmp_path):
    not_audio = tmp_path / "broken.m4a"
    not_audio.write_bytes(b"not a media file at all")

    with pytest.raises(MuxError):
        mux_tracks(h264, not_audio, tmp_path / "out.mp4", ContainerChoice("mp4", ".mp4"))

    assert list(tmp_path.glob("*.muxing.tmp")) == []


def test_an_existing_file_survives_a_failed_mux(h264, tmp_path):
    """The target is replaced once, at the end, or not at all."""
    target = tmp_path / "out.mp4"
    target.write_bytes(b"a finished download from yesterday")
    not_audio = tmp_path / "broken.m4a"
    not_audio.write_bytes(b"not a media file at all")

    with pytest.raises(MuxError):
        mux_tracks(h264, not_audio, target, ContainerChoice("mp4", ".mp4"))

    assert target.read_bytes() == b"a finished download from yesterday"


def test_an_impossible_container_is_reported_rather_than_re_encoded(vp9, aac, tmp_path):
    """Asking for AAC in WebM is the case the container rule exists to avoid."""
    with pytest.raises(MuxError):
        mux_tracks(vp9, aac, tmp_path / "out.webm", ContainerChoice("webm", ".webm"))
