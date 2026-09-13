"""The wire form: what survives the trip, and what must not make it onto it.

`test_job_snapshot.py` pins what a snapshot contains. This pins what happens to
it on the way out - which is where a value that looked harmless in Python turns
out to have no JSON at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from video_downloader.domain.download_job import DownloadJob, LifecycleState, ProgressUnit
from video_downloader.domain.job_error import ErrorKind, JobError
from video_downloader.domain.job_snapshot import JobSnapshot
from video_downloader.host import protocol


def a_job(tmp_path: Path) -> DownloadJob:
    job = DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)
    job.title = "Ein Video mit Umlauten"
    job.output_file = tmp_path / "Ein Video.mp4"
    job.progress_unit = ProgressUnit.BYTES
    job.update_progress(512, 2048)
    job.transition(LifecycleState.DOWNLOADING)
    job.expected_bytes = 2048
    return job


# --- framing ---------------------------------------------------------------


def test_a_message_survives_the_round_trip():
    message = {"type": "jobs.add", "id": 7, "url": "https://provider.test/x"}

    assert protocol.decode(protocol.encode(message)) == message


def test_one_message_is_one_line():
    encoded = protocol.encode({"type": "x", "note": "no newlines inside"})

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1


def test_non_ascii_travels_as_itself():
    """Escaping would only make the stream harder to read; it is UTF-8 anyway."""
    encoded = protocol.encode({"type": "x", "title": "Größe"})

    assert "Größe".encode("utf-8") in encoded


@pytest.mark.parametrize(
    "line, why",
    [
        (b"", "empty"),
        (b"   \n", "blank"),
        (b"{ not json\n", "not JSON"),
        (b'["a list"]\n', "not an object"),
        (b'{"id": 1}\n', "no type"),
    ],
)
def test_a_line_that_is_not_a_message_says_so(line, why):
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(line)


# --- payloads --------------------------------------------------------------


def test_a_snapshot_payload_is_json(tmp_path):
    payload = protocol.job_payload(a_job(tmp_path))

    # The real check: it must round-trip through JSON without a custom encoder.
    assert json.loads(json.dumps(payload)) == payload


def test_a_snapshot_payload_carries_every_field_of_the_snapshot(tmp_path):
    """A field added to the snapshot must not fall off the wire unnoticed."""
    snapshot = JobSnapshot.of(a_job(tmp_path))
    payload = protocol.snapshot_payload(snapshot)

    # Snapshot names are snake_case, wire names are camelCase; compare on the
    # shape rather than the spelling.
    assert len(payload) == len(snapshot.__dataclass_fields__)


def test_a_path_becomes_a_string_and_an_enum_its_value(tmp_path):
    payload = protocol.job_payload(a_job(tmp_path))

    assert payload["outputFile"] == str(tmp_path / "Ein Video.mp4")
    assert payload["state"] == "downloading"
    assert payload["unit"] == "bytes"
    assert payload["hasKnownTotal"] is True


def test_no_job_file_is_null_rather_than_the_string_none(tmp_path):
    job = DownloadJob(url="u", quality="best", output_dir=tmp_path)

    assert protocol.job_payload(job)["outputFile"] is None


def test_an_error_travels_without_its_exception(tmp_path):
    job = a_job(tmp_path)
    job.fail(
        JobError(
            kind=ErrorKind.TRACK,
            code="TrackDownloadError",
            message="die Audiospur kam nicht an",
            retryable=True,
            cause=RuntimeError("the real cause"),
        )
    )

    payload = protocol.job_payload(job)

    assert payload["error"]["kind"] == "track"
    assert payload["error"]["code"] == "TrackDownloadError"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["text"] == "TrackDownloadError: die Audiospur kam nicht an"
    assert "cause" not in payload["error"]
    assert json.loads(json.dumps(payload)) == payload


def test_a_job_that_did_not_fail_carries_a_null_error(tmp_path):
    assert protocol.job_payload(a_job(tmp_path))["error"] is None


# --- handshake -------------------------------------------------------------


def test_the_handshake_is_one_json_line_naming_the_version():
    line = protocol.handshake(54321, "cafe")

    assert "\n" not in line
    assert json.loads(line) == {
        "protocol": protocol.PROTOCOL_VERSION,
        "port": 54321,
        "token": "cafe",
    }


def test_every_command_constant_is_in_the_command_set():
    """A command the server dispatches but the set does not name is a typo."""
    named = {
        value
        for key, value in vars(protocol).items()
        if key.isupper() and isinstance(value, str) and value.startswith(("jobs.", "settings.", "sessions.", "app.", "ask.reply", "hello"))
    }

    assert named <= protocol.COMMANDS
