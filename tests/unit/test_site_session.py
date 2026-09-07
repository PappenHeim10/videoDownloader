"""A site session: what it is, and what keeping one costs.

Every test here is about a credential, so they are written against the two
things that matter about one - that it survives a restart for the account that
created it and nobody else, and that it never turns up somewhere it can be read
by accident. The value's `__repr__` gets its own test for that second reason: a
dataclass repr would have put an account's session token into the first log line
or exception that happened to carry the object.

Nothing here touches the developer's real per-user directory: `HOME_ENV_VAR`
points the paths at a temporary root for every test.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from video_downloader.domain.site_session import SessionCookie, SiteSession
from video_downloader.infrastructure import secret_store
from video_downloader.infrastructure.paths import HOME_ENV_VAR, AppPaths
from video_downloader.infrastructure.session_store import SessionStore

TOKEN = "a-session-token-nobody-may-read"


@pytest.fixture
def store(tmp_path, monkeypatch) -> SessionStore:
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    return SessionStore()


def session_for(site: str = "x.com", token: str = TOKEN) -> SiteSession:
    return SiteSession.now(site, (
        SessionCookie("auth_token", token, ".x.com"),
        SessionCookie("ct0", "csrf-value", ".x.com"),
    ))


class _RefusingSecrets:
    """A platform with nothing to protect bytes with."""

    SecretStoreError = secret_store.SecretStoreError

    @staticmethod
    def available() -> bool:
        return False

    @staticmethod
    def protect(data: bytes) -> bytes:  # pragma: no cover - never reached
        raise AssertionError("must not be called when nothing is available")

    @staticmethod
    def unprotect(blob: bytes) -> bytes:  # pragma: no cover - never reached
        raise AssertionError("must not be called when nothing is available")


class _ForeignSecrets:
    """A store whose blobs were written by another account or machine."""

    @staticmethod
    def available() -> bool:
        return True

    @staticmethod
    def protect(data: bytes) -> bytes:
        return b"foreign:" + data

    @staticmethod
    def unprotect(blob: bytes) -> bytes:
        raise secret_store.SecretStoreError(
            "CryptUnprotectData failed with Windows error 13"
        )


# --- the value ---------------------------------------------------------------


def test_a_session_never_prints_its_own_values():
    """The one property that keeps a token out of a log it was never meant for."""
    printed = repr(session_for())

    assert TOKEN not in printed
    assert "csrf-value" not in printed
    assert "auth_token" in printed and "ct0" in printed
    assert "<redacted>" in printed


def test_a_cookie_never_prints_its_own_value():
    printed = repr(SessionCookie("auth_token", TOKEN, ".x.com"))

    assert TOKEN not in printed
    assert ".x.com" in printed


def test_a_session_missing_a_cookie_is_not_a_session():
    """What "signed in" means is the site's answer, not a subset of it."""
    half = SiteSession.now("x.com", (SessionCookie("ct0", "csrf-value", ".x.com"),))

    assert not half.has_all(("auth_token", "ct0"))
    assert session_for().has_all(("auth_token", "ct0"))


def test_a_cookie_with_no_value_does_not_count_as_present():
    emptied = SiteSession.now("x.com", (
        SessionCookie("auth_token", "", ".x.com"),
        SessionCookie("ct0", "csrf-value", ".x.com"),
    ))

    assert not emptied.has_all(("auth_token", "ct0"))


# --- keeping one -------------------------------------------------------------


def test_a_saved_session_comes_back_after_a_restart(store):
    assert store.load("x.com") is None

    assert store.save(session_for()) is True

    # A second store is the restart: nothing is shared but the file.
    reopened = SessionStore(paths=store.paths)
    restored = reopened.load("x.com")
    assert restored is not None
    assert [(c.name, c.value, c.domain) for c in restored.cookies] == [
        ("auth_token", TOKEN, ".x.com"),
        ("ct0", "csrf-value", ".x.com"),
    ]


def test_the_stored_file_contains_no_readable_token(store):
    store.save(session_for())

    written = store.paths.session_file.read_bytes()

    assert TOKEN.encode() not in written
    assert b"auth_token" not in written
    # And it is not in the file the user edits by hand either.
    assert not store.paths.config_file.exists()


def test_signing_out_removes_the_file_as_well_as_the_session(store):
    store.save(session_for())

    assert store.clear("x.com") is True

    assert store.load("x.com") is None
    assert not store.paths.session_file.exists()
    assert store.clear("x.com") is False


def test_a_session_from_another_account_is_no_session_at_all(tmp_path, monkeypatch, caplog):
    """Copying the file to another machine has to yield nothing, not an error.

    The user can act on "sign in again"; they cannot act on a decryption
    failure, and the application must not stop working because of one.
    """
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "appdata"))
    paths = AppPaths.default()
    paths.ensure_root()
    paths.session_file.write_bytes(b"a blob this account cannot read")

    store = SessionStore(paths=paths, secrets=_ForeignSecrets())
    with caplog.at_level("WARNING"):
        assert store.load("x.com") is None

    assert "Konto" in caplog.text


def test_a_corrupt_store_is_no_session_at_all(store):
    store.paths.ensure_root()
    store.paths.session_file.write_bytes(secret_store.protect(b"{not json"))

    assert SessionStore(paths=store.paths).load("x.com") is None


def test_an_entry_of_an_older_shape_is_skipped_rather_than_trusted(store):
    store.paths.ensure_root()
    store.paths.session_file.write_bytes(secret_store.protect(json.dumps({
        "x.com": {"cookies": [{"name": "auth_token"}]},          # no value, no domain
        "youtube.com": {"cookies": "not a list"},
        "peertube.test": {"captured_at": "not a date", "cookies": [
            {"name": "auth_token", "value": "v", "domain": ".peertube.test"},
        ]},
    }).encode()))

    reopened = SessionStore(paths=store.paths)

    assert reopened.load("x.com") is None
    assert reopened.load("youtube.com") is None
    # The one entry that carries a whole cookie survives, and a date nobody can
    # read costs the timestamp rather than the session.
    kept = reopened.load("peertube.test")
    assert kept is not None
    assert kept.captured_at <= datetime.now(timezone.utc)


def test_without_a_protected_store_a_session_lasts_only_for_the_run(store, caplog):
    """A plaintext token in a file that looks protected is the worse answer."""
    memory_only = SessionStore(paths=store.paths, secrets=_RefusingSecrets())

    with caplog.at_level("INFO"):
        assert memory_only.save(session_for()) is False

    assert memory_only.persists is False
    # Usable now - the resolution this run needs it - and gone with the process.
    assert memory_only.load("x.com") is not None
    assert not store.paths.session_file.exists()
    assert SessionStore(paths=store.paths).load("x.com") is None


def test_sites_lists_what_is_signed_in(store):
    store.save(session_for("x.com"))
    store.save(session_for("twitter.test"))

    assert store.sites() == ("twitter.test", "x.com")
