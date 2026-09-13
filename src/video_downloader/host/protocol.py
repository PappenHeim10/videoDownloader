"""What the core and a front end say to each other, and how it is spelled.

One JSON object per line, UTF-8, newline-terminated. No framing header, no
length prefix: a line is a message, which makes the stream readable in a
terminal and debuggable with nothing but `nc`.

Three kinds of message, and the difference between them is who is waiting:

* **Command** - front end to core, carries an `id`, and is answered by exactly
  one `result` with the same `id`.
* **Event** - core to front end, carries no `id`, and nobody answers it.
* **Ask** - core to front end, carries an `askId`, and the core is blocked on
  the answer. There are two, and both existed as callables before there was a
  protocol: the large-download question and the login request.

`PROTOCOL_VERSION` is stated in the handshake rather than negotiated. A front
end that does not recognise the number should refuse to start rather than guess
what changed; there is exactly one core and one front end, and a version skew
between them is a packaging fault, not a situation to recover from.

Nothing here imports Qt, and nothing imports the transport either - this module
is the vocabulary, `server` is the mouth.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from video_downloader.domain.job_error import JobError
from video_downloader.domain.job_snapshot import JobSnapshot

PROTOCOL_VERSION = 1

# --- commands (front end -> core) -------------------------------------------

HELLO = "hello"
JOBS_LIST = "jobs.list"
JOBS_ADD = "jobs.add"
JOBS_CANCEL = "jobs.cancel"
JOBS_DELETE = "jobs.delete"
JOBS_RESCAN = "jobs.rescan"
SETTINGS_GET = "settings.get"
SETTINGS_SET_DIRECTORY = "settings.setDownloadDirectory"
SESSIONS_LIST = "sessions.list"
SESSIONS_PUT = "sessions.put"
SESSIONS_CLEAR = "sessions.clear"
APP_SHUTDOWN = "app.shutdown"
ASK_REPLY = "ask.reply"

#: Every command the core answers. A message with any other `type` is refused
#: by name rather than ignored, so a typo in a front end is a visible failure.
COMMANDS = frozenset({
    HELLO, JOBS_LIST, JOBS_ADD, JOBS_CANCEL, JOBS_DELETE, JOBS_RESCAN,
    SETTINGS_GET, SETTINGS_SET_DIRECTORY, SESSIONS_LIST, SESSIONS_PUT,
    SESSIONS_CLEAR, APP_SHUTDOWN, ASK_REPLY,
})

# --- answers, events and asks (core -> front end) ---------------------------

RESULT = "result"
JOB_CREATED = "job.created"
JOB_CHANGED = "job.changed"
JOB_REMOVED = "job.removed"
ASK_CONFIRM_LARGE_DOWNLOAD = "ask.confirmLargeDownload"
ASK_LOGIN = "ask.login"

# --- error codes ------------------------------------------------------------

#: Named so a front end can branch on them. The message stays human-readable;
#: the code is what a program reads.
E_UNAUTHENTICATED = "unauthenticated"
E_UNKNOWN_COMMAND = "unknown_command"
E_BAD_REQUEST = "bad_request"
E_NOT_FOUND = "not_found"
E_FAILED = "failed"


class ProtocolError(Exception):
    """A line that is not a message this protocol defines."""


def encode(message: dict[str, Any]) -> bytes:
    """One message as the bytes that carry it.

    `ensure_ascii=False` because titles carry umlauts and emoji and the stream
    is UTF-8 anyway; escaping them would only make the wire harder to read.
    """
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    """One line back into a message, or `ProtocolError`.

    A blank line is not a message and says so, rather than arriving as an empty
    dict that every caller would then have to check for.
    """
    text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        raise ProtocolError("empty line")
    try:
        message = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"not JSON: {error}") from error
    if not isinstance(message, dict):
        raise ProtocolError(f"not an object: {type(message).__name__}")
    if not isinstance(message.get("type"), str):
        raise ProtocolError("message has no type")
    return message


# --- payloads ---------------------------------------------------------------


def error_payload(error: JobError | None) -> dict[str, Any] | None:
    """A failure as the front end sees it - without the exception behind it.

    `cause` stays on the job for the log. It is a live Python object and has no
    business on a wire.
    """
    if error is None:
        return None
    return {
        "kind": str(error.kind),
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "text": str(error),
    }


def snapshot_payload(snapshot: JobSnapshot) -> dict[str, Any]:
    """A job snapshot as JSON.

    `outputFile` becomes a string because JSON has no path type. That is the
    one conversion happening here; everything else is already a value the
    format can carry, which is what `JobSnapshot` was shaped for.
    """
    return {
        "id": snapshot.id,
        "url": snapshot.url,
        "title": snapshot.title,
        "state": str(snapshot.state),
        "progress": snapshot.progress,
        "done": snapshot.done,
        "total": snapshot.total,
        "unit": str(snapshot.unit),
        "hasKnownTotal": snapshot.has_known_total,
        "outputFile": str(snapshot.output_file) if snapshot.output_file else None,
        "expectedBytes": snapshot.expected_bytes,
        "error": error_payload(snapshot.error),
        "fromDisk": snapshot.from_disk,
        "seq": snapshot.seq,
    }


def job_payload(job: Any) -> dict[str, Any]:
    """Shorthand for the snapshot of a live job."""
    return snapshot_payload(JobSnapshot.of(job))


def result(message_id: Any, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": RESULT, "id": message_id, "ok": True, "data": data or {}}


def failure(message_id: Any, code: str, message: str) -> dict[str, Any]:
    return {
        "type": RESULT,
        "id": message_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def handshake(port: int, token: str) -> str:
    """The one line the core prints on stdout before it goes quiet.

    Everything after this travels over the socket. The line is what tells the
    front end where to connect and proves it is talking to the process it
    started, rather than to whatever else happens to hold that port.
    """
    return json.dumps(
        {"protocol": PROTOCOL_VERSION, "port": port, "token": token},
        separators=(",", ":"),
    )


def directory_payload(directory: Path | None) -> dict[str, Any]:
    return {"downloadDirectory": str(directory) if directory else None}
