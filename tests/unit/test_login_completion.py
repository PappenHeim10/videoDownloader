"""What counts as a session, checked without a browser.

The same three rules `tests/integration/test_login_window.py` pins through a Qt
dialog - only here they are the subject rather than a side effect, and nothing
needs a `QApplication` to run. That matters because the rule is about
credentials: getting it wrong stores a session that does not work, and the
failure surfaces as "every download from that site fails" long afterwards.

The names below are X's, because X is the site that made the rule necessary.
Nothing in the collector knows that.
"""

from __future__ import annotations

from video_downloader.application.login_completion import LoginCollector

REQUIRED = ("auth_token", "ct0")


def a_collector() -> LoginCollector:
    return LoginCollector("x.com", REQUIRED)


# --- what is kept ------------------------------------------------------------


def test_only_the_required_names_are_kept():
    """A site sets a lot on the way in; none of it means "signed in"."""
    collector = a_collector()

    for name in ("guest_id", "gt", "__cf_bm", "personalization_id"):
        assert collector.note(name, "irrelevant", ".x.com") is False

    collector.note("auth_token", "token", ".x.com")
    collector.note("ct0", "csrf", ".x.com")

    session = collector.session()
    assert session is not None
    assert session.names() == REQUIRED


def test_the_last_value_of_a_name_wins():
    """X rotates `ct0` in the same exchange that issues `auth_token`."""
    collector = a_collector()
    collector.note("ct0", "csrf-before-login", ".x.com")
    collector.note("auth_token", "token", ".x.com")
    collector.note("ct0", "csrf-after-login", ".x.com")

    session = collector.session()
    assert [cookie.value for cookie in session.cookies] == ["token", "csrf-after-login"]


def test_the_domain_travels_with_the_cookie():
    """A site sets its session on `.x.com` while the API lives on `api.x.com`."""
    collector = a_collector()
    collector.note("auth_token", "token", ".x.com")
    collector.note("ct0", "csrf", ".x.com")

    assert {cookie.domain for cookie in collector.session().cookies} == {".x.com"}


# --- when it is complete -----------------------------------------------------


def test_one_cookie_of_a_pair_is_not_a_session():
    collector = a_collector()
    collector.note("ct0", "csrf-before-login", ".x.com")

    assert collector.is_complete is False
    assert collector.session() is None


def test_a_withdrawn_cookie_undoes_completeness():
    """A login that does not hold must not leave a session behind."""
    collector = a_collector()
    collector.note("auth_token", "token", ".x.com")
    collector.note("ct0", "csrf", ".x.com")
    assert collector.is_complete is True

    assert collector.forget("auth_token") is True

    assert collector.is_complete is False
    assert collector.session() is None


def test_forgetting_something_never_held_is_not_an_event():
    assert a_collector().forget("auth_token") is False


def test_an_empty_value_does_not_count_as_present():
    """A cleared cookie is set to the empty string rather than removed."""
    collector = a_collector()
    collector.note("auth_token", "", ".x.com")
    collector.note("ct0", "csrf", ".x.com")

    assert collector.is_complete is False


# --- shape -------------------------------------------------------------------


def test_the_session_follows_the_required_order_not_the_arrival_order():
    """Two logins of the same site produce the same shape."""
    arrived_backwards = a_collector()
    arrived_backwards.note("ct0", "csrf", ".x.com")
    arrived_backwards.note("auth_token", "token", ".x.com")

    assert arrived_backwards.session().names() == REQUIRED


def test_names_reports_only_what_is_held():
    collector = a_collector()
    assert collector.names == ()

    collector.note("ct0", "csrf", ".x.com")
    assert collector.names == ("ct0",)

    collector.note("auth_token", "token", ".x.com")
    assert collector.names == REQUIRED


def test_the_session_belongs_to_the_site_it_was_collected_for():
    collector = LoginCollector("example.test", ("sid",))
    collector.note("sid", "value", ".example.test")

    assert collector.session().site == "example.test"


def test_a_site_with_one_required_cookie_needs_only_that_one():
    """Nothing here is specific to X's pair."""
    collector = LoginCollector("example.test", ("sid",))
    assert collector.is_complete is False

    collector.note("sid", "value", ".example.test")

    assert collector.is_complete is True
