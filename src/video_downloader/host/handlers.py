"""What each command does. One method per message type, and nothing else.

Every handler is a thin translation between a JSON object and a call that
already existed - `manager.add_download`, `settings.set_download_directory`,
`sessions.clear`. That thinness is the point: the protocol must not become a
second place where the application's rules live, or the two will disagree.

The two per-job callables are built here as well, because this is the only
layer that knows both the job and the connection. In process they were supplied
by the window; over a connection they are supplied by whoever is on the other
end of it, and the shape of both is unchanged - one awaits a bool, the other
awaits a bool and has a side effect on the session store.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from video_downloader.application.download_directory import DownloadDirectory
from video_downloader.application.download_manager import DownloadManager
from video_downloader.domain.download_job import DownloadJob
from video_downloader.domain.site_session import SessionCookie, SiteSession
from video_downloader.host import protocol
from video_downloader.host.asks import (
    CONFIRM_TIMEOUT_SECONDS,
    LOGIN_TIMEOUT_SECONDS,
    AskRegistry,
    AskUnavailable,
)
from video_downloader.host.events import JobEventPublisher
from video_downloader.host.logins import LoginRegistry
from video_downloader.infrastructure.session_store import SessionStore
from video_downloader.infrastructure.settings import AppSettings

logger = logging.getLogger(__name__)

#: What a login ask resolves with when the core itself finished it. An object
#: rather than `True`, so that a front end replying `true` to a login cannot
#: pass for one that produced a session.
_SIGNED_IN = object()


class CommandError(Exception):
    """A command that cannot be carried out, with the code the front end reads."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CommandHandlers:
    def __init__(
        self,
        *,
        manager: DownloadManager,
        settings: AppSettings,
        sessions: SessionStore,
        publisher: JobEventPublisher,
        asks: AskRegistry,
        send: Callable[[dict[str, Any]], bool],
        request_shutdown: Callable[[], None],
        site_logins: Iterable[Any] = (),
    ) -> None:
        self.manager = manager
        self.settings = settings
        # The same rule the window uses. A front end over a connection has
        # already done its own asking, so it only ever reaches `set`.
        self.directory = DownloadDirectory(settings)
        self.sessions = sessions
        self.publisher = publisher
        self.asks = asks
        self._send = send
        self._request_shutdown = request_shutdown
        # The sites a login can be started for, keyed by site. Each entry has
        # the three attributes of `ProviderLoginRequired`; which providers
        # those are is decided where the providers are composed, not here.
        self.site_logins = {login.site: login for login in site_logins}
        self.logins = LoginRegistry(self._signed_in)

    # --- jobs --------------------------------------------------------------

    async def jobs_list(self, _message: dict[str, Any]) -> dict[str, Any]:
        jobs = self.manager.get_jobs()
        self.publisher.attach_all(jobs)
        return {"jobs": [protocol.job_payload(job) for job in jobs]}

    async def jobs_add(self, message: dict[str, Any]) -> dict[str, Any]:
        url = str(message.get("url") or "").strip()
        if not url:
            raise CommandError(protocol.E_BAD_REQUEST, "jobs.add needs a url")

        quality = message.get("quality") or "best"
        raw_directory = message.get("outputDir")
        directory = Path(raw_directory) if raw_directory else None
        if directory is None and self.manager.output_dir is None:
            raise CommandError(
                protocol.E_BAD_REQUEST,
                "no download directory is configured; pass outputDir or set one",
            )

        try:
            job = self.manager.add_download(
                url,
                quality,
                output_dir=directory,
                confirm_large_download=self.confirm_large_download,
                request_login=self.request_login,
            )
        except (RuntimeError, ValueError, OSError) as error:
            raise CommandError(protocol.E_FAILED, str(error)) from error

        self.publisher.attach(job, announce=True)
        return {"job": protocol.job_payload(job)}

    async def jobs_cancel(self, message: dict[str, Any]) -> dict[str, Any]:
        job = self._job(message)
        await self.manager.cancel_download(job)
        return {"job": protocol.job_payload(job)}

    async def jobs_delete(self, message: dict[str, Any]) -> dict[str, Any]:
        job = self._job(message)
        if "deleteFile" not in message:
            # Same rule as the manager's keyword without a default: for an entry
            # found on disk this removes a real video, so it is never implied.
            raise CommandError(
                protocol.E_BAD_REQUEST, "jobs.delete needs an explicit deleteFile"
            )
        await self.manager.delete_download(job, delete_file=bool(message["deleteFile"]))
        self.publisher.detach(job, announce=True)
        return {}

    async def jobs_rescan(self, _message: dict[str, Any]) -> dict[str, Any]:
        before = {job.id for job in self.manager.get_jobs()}
        self.manager.rescan_output_directory()
        jobs = self.manager.get_jobs()

        # The scan drops the entries of the previous directory and adds the ones
        # it found. The front end rebuilds from `jobs`, but the removals still
        # go out so anything watching a single job learns it is gone.
        for job_id in before - {job.id for job in jobs}:
            self.publisher.forget(job_id)
            self.publisher.announce_removed(job_id)
        self.publisher.attach_all(jobs)
        return {"jobs": [protocol.job_payload(job) for job in jobs]}

    def _job(self, message: dict[str, Any]) -> DownloadJob:
        job_id = message.get("jobId")
        for job in self.manager.get_jobs():
            if job.id == job_id:
                return job
        raise CommandError(protocol.E_NOT_FOUND, f"no job with id {job_id!r}")

    # --- settings ----------------------------------------------------------

    async def settings_get(self, _message: dict[str, Any]) -> dict[str, Any]:
        return protocol.directory_payload(self.directory.current)

    async def settings_set_directory(self, message: dict[str, Any]) -> dict[str, Any]:
        raw = message.get("path")
        if not raw:
            raise CommandError(
                protocol.E_BAD_REQUEST, "settings.setDownloadDirectory needs a path"
            )
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_dir():
            raise CommandError(protocol.E_BAD_REQUEST, f"not a directory: {candidate}")
        directory = self.directory.set(candidate)
        # Future jobs only, exactly as the window has always behaved - but the
        # scan follows, because otherwise the list keeps describing the old one.
        self.manager.rescan_output_directory(directory)
        return protocol.directory_payload(directory)

    # --- sessions ----------------------------------------------------------

    async def sessions_list(self, _message: dict[str, Any]) -> dict[str, Any]:
        return {
            "sites": list(self.sessions.sites()),
            "persists": self.sessions.persists,
            "logins": [protocol.site_login_payload(login) for login in self.site_logins.values()],
        }

    async def sessions_put(self, message: dict[str, Any]) -> dict[str, Any]:
        session = _session_from(message)
        if session is None:
            raise CommandError(
                protocol.E_BAD_REQUEST, "sessions.put needs a site and named cookies"
            )
        return {"persisted": self.sessions.save(session)}

    async def sessions_clear(self, message: dict[str, Any]) -> dict[str, Any]:
        site = str(message.get("site") or "")
        if not site:
            raise CommandError(protocol.E_BAD_REQUEST, "sessions.clear needs a site")
        return {"removed": self.sessions.clear(site)}

    # --- logins ------------------------------------------------------------

    async def login_start(self, message: dict[str, Any]) -> dict[str, Any]:
        """Start a login the user asked for, rather than one a job needed."""
        site = str(message.get("site") or "")
        login = self.site_logins.get(site)
        if login is None:
            raise CommandError(protocol.E_NOT_FOUND, f"no login is known for {site!r}")
        login_id = self.logins.begin(login.site, login.required_cookies)
        return {"loginId": login_id, **protocol.site_login_payload(login)}

    async def login_observe(self, message: dict[str, Any]) -> dict[str, Any]:
        login_id = str(message.get("loginId") or "")
        cookies = message.get("cookies")
        if not login_id or not isinstance(cookies, list):
            raise CommandError(
                protocol.E_BAD_REQUEST, "login.observe needs a loginId and a cookie list"
            )
        return {"active": self.logins.observe(login_id, cookies)}

    async def login_cancel(self, message: dict[str, Any]) -> dict[str, Any]:
        login_id = str(message.get("loginId") or "")
        cancelled = self.logins.discard(login_id)
        # A login a job asked for is also an open question; ending the login
        # without ending the question would leave the job waiting for its
        # timeout with nothing left to answer it.
        self.asks.resolve(login_id, None)
        return {"cancelled": cancelled}

    def _signed_in(self, login_id: str, session: SiteSession) -> None:
        """A login settled. Keep the session, and tell whoever showed the page."""
        persisted = self.sessions.save(session)
        logger.info(
            "Signed in to %s (%s); %s",
            session.site,
            ", ".join(session.names()),
            "stored" if persisted else "this run only",
        )
        self._send({
            "type": protocol.LOGIN_FINISHED,
            "loginId": login_id,
            "site": session.site,
            "signedIn": True,
            "persisted": persisted,
        })
        # A no-op unless a job is waiting on this login.
        self.asks.resolve(login_id, _SIGNED_IN)

    # --- application -------------------------------------------------------

    async def app_shutdown(self, _message: dict[str, Any]) -> dict[str, Any]:
        self._request_shutdown()
        return {}

    # --- the two questions -------------------------------------------------

    async def confirm_large_download(self, job: DownloadJob, estimated_bytes: int) -> bool:
        """Ask before a download large enough that the answer still matters.

        No answer means no. The alternative - starting several gigabytes because
        the window that was supposed to ask is gone - is the failure this
        question exists to prevent.
        """
        try:
            answer = await self.asks.ask(
                protocol.ASK_CONFIRM_LARGE_DOWNLOAD,
                {"jobId": job.id, "title": job.title or job.url,
                 "estimatedBytes": estimated_bytes},
                CONFIRM_TIMEOUT_SECONDS,
            )
        except AskUnavailable as error:
            logger.warning("[JOB %s] Size question unanswered: %s", job.id, error)
            return False
        return bool(answer)

    async def request_login(self, refusal: Exception) -> bool:
        """Have the front end show a site's login, and wait until it settles.

        The front end shows the page and reports its cookies; the decision that
        they add up to a session, and storing it, happen here. A front end
        never touches the session store and cannot store a session for a site
        it was not asked about. Its only answer to the question itself is
        `null`, for a user who closed the window.
        """
        site = getattr(refusal, "site", "")
        login_id = self.logins.begin(site, getattr(refusal, "required_cookies", ()))
        try:
            answer = await self.asks.ask(
                protocol.ASK_LOGIN,
                {"loginId": login_id, **protocol.site_login_payload(refusal)},
                LOGIN_TIMEOUT_SECONDS,
                ask_id=login_id,
            )
        except AskUnavailable as error:
            logger.info("Login request for %s unanswered: %s", site, error)
            answer = None
        finally:
            if self.logins.discard(login_id):
                # Still open means it ended without a session - a timeout, or
                # the front end went away. A window still showing the page
                # should close rather than go on collecting for nobody.
                self._send({
                    "type": protocol.LOGIN_FINISHED,
                    "loginId": login_id,
                    "site": site,
                    "signedIn": False,
                    "persisted": False,
                })

        if answer is not _SIGNED_IN:
            logger.info("Login at %s was cancelled", site)
            return False
        return True


def _session_from(message: dict[str, Any]) -> SiteSession | None:
    """A site session out of a message, or `None` if it is not one.

    Every field is checked rather than trusted. The values are credentials, and
    a malformed one must produce no session at all rather than a half of one -
    the same rule the store applies to what it reads back from disk.
    """
    site = str(message.get("site") or "")
    raw_cookies = message.get("cookies")
    if not site or not isinstance(raw_cookies, list):
        return None

    cookies = tuple(
        SessionCookie(name=c["name"], value=c["value"], domain=c["domain"])
        for c in raw_cookies
        if isinstance(c, dict)
        and all(isinstance(c.get(key), str) and c.get(key) for key in ("name", "value", "domain"))
    )
    if not cookies:
        return None
    return SiteSession.now(site, cookies)
