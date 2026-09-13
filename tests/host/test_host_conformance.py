"""Every command, every event and both questions, over a real socket.

Driven through `asyncio.open_connection` against a real `Host` rather than by
calling the handlers directly. The point of this file is the protocol, and a
handler test would pass while the framing, the token check or the event stream
were broken.

No network and no provider: the job runner is a double that does what a real
one does to a job - move it through its states and stop when it is told to. That
keeps the tests about the protocol and leaves the download itself to the suites
that already cover it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from video_downloader.application.download_manager import DownloadManager
from video_downloader.domain.download_job import DownloadJob, LifecycleState
from video_downloader.host import protocol
from video_downloader.host.server import Host
from video_downloader.infrastructure.paths import HOME_ENV_VAR
from video_downloader.infrastructure.session_store import SessionStore
from video_downloader.infrastructure.settings import AppSettings

URL = "https://provider.test/video"


async def _runs_until_stopped(job: DownloadJob) -> DownloadJob:
    job.transition(LifecycleState.DOWNLOADING)
    job.update_progress(1, 4)
    await job.stop_event.wait()
    job.transition(LifecycleState.CANCELLED)
    return job


class Client:
    """A front end, reduced to what the protocol needs of one."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0

    async def send(self, kind: str, **fields) -> int:
        self._next_id += 1
        message = {"type": kind, "id": self._next_id, **fields}
        self._writer.write(protocol.encode(message))
        await self._writer.drain()
        return self._next_id

    async def receive(self, timeout: float = 5.0) -> dict:
        line = await asyncio.wait_for(self._reader.readline(), timeout)
        assert line, "the core closed the connection"
        return json.loads(line.decode("utf-8"))

    async def result_for(self, message_id: int, timeout: float = 5.0) -> dict:
        """The answer to one command, skipping the events that overtake it."""
        while True:
            message = await self.receive(timeout)
            if message.get("type") == protocol.RESULT and message.get("id") == message_id:
                return message

    async def wait_for(self, kind: str, timeout: float = 5.0) -> dict:
        while True:
            message = await self.receive(timeout)
            if message.get("type") == kind:
                return message

    async def call(self, kind: str, **fields) -> dict:
        """Send a command and return its result. Fails loudly on a refusal."""
        answer = await self.result_for(await self.send(kind, **fields))
        assert answer["ok"], answer.get("error")
        return answer["data"]

    def close(self) -> None:
        self._writer.close()


@pytest.fixture
def videos(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    directory = tmp_path / "videos"
    directory.mkdir()
    return directory


@pytest_asyncio.fixture
async def host(videos):
    settings = AppSettings()
    settings.set_download_directory(videos)
    manager = DownloadManager(videos, job_runner=_runs_until_stopped)
    served = Host(manager=manager, settings=settings, sessions=SessionStore())
    await served.start()
    try:
        yield served
    finally:
        await served.stop()


async def connect(host: Host, *, token: str | None = None) -> Client:
    reader, writer = await asyncio.open_connection("127.0.0.1", host.port)
    client = Client(reader, writer)
    message_id = await client.send(protocol.HELLO, token=token if token is not None else host.token)
    answer = await client.result_for(message_id)
    assert answer["ok"], answer.get("error")
    assert answer["data"]["protocol"] == protocol.PROTOCOL_VERSION
    return client


@pytest_asyncio.fixture
async def client(host):
    connected = await connect(host)
    try:
        yield connected
    finally:
        connected.close()


# --- the handshake ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_handshake_names_the_protocol_and_the_jobs(host):
    client = await connect(host)
    try:
        assert True  # connect() already asserted both
    finally:
        client.close()


@pytest.mark.asyncio
async def test_a_wrong_token_is_refused(host):
    reader, writer = await asyncio.open_connection("127.0.0.1", host.port)
    client = Client(reader, writer)
    try:
        message_id = await client.send(protocol.HELLO, token="not the token")
        answer = await client.result_for(message_id)

        assert answer["ok"] is False
        assert answer["error"]["code"] == protocol.E_UNAUTHENTICATED
    finally:
        client.close()


@pytest.mark.asyncio
async def test_a_command_before_hello_is_refused(host):
    reader, writer = await asyncio.open_connection("127.0.0.1", host.port)
    client = Client(reader, writer)
    try:
        message_id = await client.send(protocol.JOBS_LIST)
        answer = await client.result_for(message_id)

        assert answer["error"]["code"] == protocol.E_UNAUTHENTICATED
    finally:
        client.close()


@pytest.mark.asyncio
async def test_an_unknown_command_is_named_rather_than_ignored(client):
    message_id = await client.send("jobs.levitate")
    answer = await client.result_for(message_id)

    assert answer["error"]["code"] == protocol.E_UNKNOWN_COMMAND
    assert "jobs.levitate" in answer["error"]["message"]


@pytest.mark.asyncio
async def test_an_unreadable_line_does_not_kill_the_connection(client):
    client._writer.write(b"{ this is not json\n")
    await client._writer.drain()
    refusal = await client.wait_for(protocol.RESULT)
    assert refusal["error"]["code"] == protocol.E_BAD_REQUEST

    # And the connection still works afterwards.
    assert "jobs" in await client.call(protocol.JOBS_LIST)


# --- jobs -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adding_a_job_answers_with_it_and_announces_it(client):
    message_id = await client.send(protocol.JOBS_ADD, url=URL, quality="best")

    created = await client.wait_for(protocol.JOB_CREATED)
    assert created["job"]["url"] == URL
    assert created["job"]["state"] in {"queued", "downloading"}

    answer = await client.result_for(message_id)
    assert answer["ok"]
    assert answer["data"]["job"]["id"] == created["job"]["id"]


@pytest.mark.asyncio
async def test_a_job_without_a_url_is_refused(client):
    message_id = await client.send(protocol.JOBS_ADD, url="   ")
    answer = await client.result_for(message_id)

    assert answer["error"]["code"] == protocol.E_BAD_REQUEST


@pytest.mark.asyncio
async def test_progress_arrives_as_events(client):
    await client.send(protocol.JOBS_ADD, url=URL)

    while True:
        changed = await client.wait_for(protocol.JOB_CHANGED)
        if changed["job"]["done"]:
            break

    assert changed["job"]["done"] == 1
    assert changed["job"]["total"] == 4
    assert changed["job"]["hasKnownTotal"] is True
    assert changed["job"]["unit"] == "segments"


@pytest.mark.asyncio
async def test_cancelling_stops_the_job_and_keeps_it_listed(client):
    data = await client.call(protocol.JOBS_ADD, url=URL)
    job_id = data["job"]["id"]

    cancelled = await client.call(protocol.JOBS_CANCEL, jobId=job_id)

    assert cancelled["job"]["state"] == "cancelled"
    listed = await client.call(protocol.JOBS_LIST)
    assert [job["id"] for job in listed["jobs"]] == [job_id]


@pytest.mark.asyncio
async def test_deleting_needs_an_explicit_decision_about_the_file(client):
    data = await client.call(protocol.JOBS_ADD, url=URL)

    message_id = await client.send(protocol.JOBS_DELETE, jobId=data["job"]["id"])
    answer = await client.result_for(message_id)

    assert answer["error"]["code"] == protocol.E_BAD_REQUEST
    assert "deleteFile" in answer["error"]["message"]


@pytest.mark.asyncio
async def test_deleting_removes_the_job_and_announces_it(client):
    data = await client.call(protocol.JOBS_ADD, url=URL)
    job_id = data["job"]["id"]

    # The removal is announced before the result, so the event is read first -
    # `call()` would discard it on its way to the answer.
    message_id = await client.send(protocol.JOBS_DELETE, jobId=job_id, deleteFile=False)

    removed = await client.wait_for(protocol.JOB_REMOVED)
    assert removed["jobId"] == job_id
    assert (await client.result_for(message_id))["ok"]
    assert await client.call(protocol.JOBS_LIST) == {"jobs": []}


@pytest.mark.asyncio
async def test_an_unknown_job_is_not_found(client):
    message_id = await client.send(protocol.JOBS_CANCEL, jobId="nope")
    answer = await client.result_for(message_id)

    assert answer["error"]["code"] == protocol.E_NOT_FOUND


@pytest.mark.asyncio
async def test_a_rescan_reports_what_is_in_the_folder(client, videos):
    (videos / "already-there.mp4").write_bytes(b"video")

    data = await client.call(protocol.JOBS_RESCAN)

    titles = [job["title"] for job in data["jobs"]]
    assert titles == ["already-there.mp4"]
    assert data["jobs"][0]["fromDisk"] is True
    assert data["jobs"][0]["progress"] == 100.0


# --- settings and sessions --------------------------------------------------


@pytest.mark.asyncio
async def test_the_download_directory_can_be_read_and_changed(client, videos, tmp_path):
    assert await client.call(protocol.SETTINGS_GET) == {
        "downloadDirectory": str(videos.resolve())
    }

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data = await client.call(protocol.SETTINGS_SET_DIRECTORY, path=str(elsewhere))

    assert data["downloadDirectory"] == str(elsewhere.resolve())


@pytest.mark.asyncio
async def test_a_directory_that_is_not_one_is_refused(client, tmp_path):
    message_id = await client.send(
        protocol.SETTINGS_SET_DIRECTORY, path=str(tmp_path / "nowhere")
    )
    answer = await client.result_for(message_id)

    assert answer["error"]["code"] == protocol.E_BAD_REQUEST


@pytest.mark.asyncio
async def test_a_session_can_be_stored_listed_and_cleared(client):
    await client.call(
        protocol.SESSIONS_PUT,
        site="x.com",
        cookies=[
            {"name": "auth_token", "value": "t", "domain": ".x.com"},
            {"name": "ct0", "value": "c", "domain": ".x.com"},
        ],
    )

    listed = await client.call(protocol.SESSIONS_LIST)
    assert "x.com" in listed["sites"]

    assert await client.call(protocol.SESSIONS_CLEAR, site="x.com") == {"removed": True}
    assert "x.com" not in (await client.call(protocol.SESSIONS_LIST))["sites"]


@pytest.mark.asyncio
async def test_a_session_without_usable_cookies_is_refused(client):
    message_id = await client.send(protocol.SESSIONS_PUT, site="x.com", cookies=[{}])
    answer = await client.result_for(message_id)

    assert answer["error"]["code"] == protocol.E_BAD_REQUEST


# --- the two questions ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_size_question_travels_and_comes_back(host, client):
    job = DownloadJob(url=URL, quality="best", output_dir=host._manager.output_dir)
    job.title = "A Very Large Video"

    asking = asyncio.ensure_future(
        host._handlers.confirm_large_download(job, 3 * 1024**3)
    )
    question = await client.wait_for(protocol.ASK_CONFIRM_LARGE_DOWNLOAD)

    assert question["estimatedBytes"] == 3 * 1024**3
    assert question["title"] == "A Very Large Video"

    await client.send(protocol.ASK_REPLY, askId=question["askId"], value=True)
    assert await asyncio.wait_for(asking, 5) is True


@pytest.mark.asyncio
async def test_a_refused_size_question_answers_no(host, client):
    job = DownloadJob(url=URL, quality="best", output_dir=host._manager.output_dir)

    asking = asyncio.ensure_future(host._handlers.confirm_large_download(job, 4 * 1024**3))
    question = await client.wait_for(protocol.ASK_CONFIRM_LARGE_DOWNLOAD)
    await client.send(protocol.ASK_REPLY, askId=question["askId"], value=False)

    assert await asyncio.wait_for(asking, 5) is False


@pytest.mark.asyncio
async def test_the_login_question_stores_what_comes_back(host, client):
    class Refusal(Exception):
        site = "x.com"
        login_url = "https://x.com/login"
        required_cookies = ("auth_token", "ct0")

    asking = asyncio.ensure_future(host._handlers.request_login(Refusal()))
    question = await client.wait_for(protocol.ASK_LOGIN)

    assert question["site"] == "x.com"
    assert question["loginUrl"] == "https://x.com/login"
    assert question["requiredCookies"] == ["auth_token", "ct0"]

    await client.send(
        protocol.ASK_REPLY,
        askId=question["askId"],
        value={
            "cookies": [
                {"name": "auth_token", "value": "t", "domain": ".x.com"},
                {"name": "ct0", "value": "c", "domain": ".x.com"},
            ]
        },
    )

    assert await asyncio.wait_for(asking, 5) is True
    assert "x.com" in (await client.call(protocol.SESSIONS_LIST))["sites"]


@pytest.mark.asyncio
async def test_a_cancelled_login_answers_no_and_stores_nothing(host, client):
    class Refusal(Exception):
        site = "x.com"
        login_url = "https://x.com/login"
        required_cookies = ("auth_token",)

    asking = asyncio.ensure_future(host._handlers.request_login(Refusal()))
    question = await client.wait_for(protocol.ASK_LOGIN)
    await client.send(protocol.ASK_REPLY, askId=question["askId"], value=None)

    assert await asyncio.wait_for(asking, 5) is False
    assert (await client.call(protocol.SESSIONS_LIST))["sites"] == []


@pytest.mark.asyncio
async def test_a_question_with_nobody_connected_answers_no(host):
    """A window that never started must not start three gigabytes on its behalf."""
    job = DownloadJob(url=URL, quality="best", output_dir=host._manager.output_dir)

    assert await host._handlers.confirm_large_download(job, 5 * 1024**3) is False


@pytest.mark.asyncio
async def test_a_disconnect_ends_every_open_question(host, client):
    job = DownloadJob(url=URL, quality="best", output_dir=host._manager.output_dir)
    asking = asyncio.ensure_future(host._handlers.confirm_large_download(job, 3 * 1024**3))
    await client.wait_for(protocol.ASK_CONFIRM_LARGE_DOWNLOAD)

    client.close()

    # No timeout is waited out: the job learns immediately that nobody is there.
    assert await asyncio.wait_for(asking, 5) is False


# --- one front end ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_front_end_is_turned_away(host, client):
    reader, writer = await asyncio.open_connection("127.0.0.1", host.port)
    try:
        assert await asyncio.wait_for(reader.read(1), 5) == b""
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_a_disconnect_lets_go_of_every_job(host, client):
    await client.call(protocol.JOBS_ADD, url=URL)
    assert host._publisher.attached_count == 1

    client.close()
    await asyncio.sleep(0.1)

    assert host._publisher.attached_count == 0
