"""The options every yt-dlp call in this application is made with.

These were the YouTube adapter's until a second provider and the download layer
needed the same ones, and they are tested apart from any provider for the same
reason they live apart from one: each is a decision about what this application
sends and what it writes down, and neither answer may depend on which website a
job came from.

The two redaction tests use sentinel values rather than realistic ones, so a
failure names exactly which part of a signed URL escaped.
"""

from __future__ import annotations

import logging

from video_downloader.domain.site_session import SessionCookie, SiteSession
from video_downloader.providers.ytdlp_options import (
    base_options,
    enabled_js_runtimes,
    install_session,
)

LOGGER_NAME = "video_downloader.providers.ytdlp_options"


class _Resolver:
    """Stands in for `YoutubeDL`, which is a cookie jar as far as this matters."""

    def __init__(self) -> None:
        from http.cookiejar import CookieJar

        self.cookiejar = CookieJar()


def test_a_session_reaches_the_requests_the_resolver_will_make():
    """The point of installing a session: the API call carries it.

    Asserted through the jar's own matching rather than by reading back what
    was inserted, because the question is not "is the cookie in the jar" but
    "does it go out with a request to the host that checks it" - X sets the
    session on `.x.com` and the endpoint that reads it is `api.x.com`.
    """
    from urllib.request import Request

    resolver = _Resolver()
    install_session(resolver, SiteSession.now("x.com", (
        SessionCookie("auth_token", "token-value", ".x.com"),
        SessionCookie("ct0", "csrf-value", ".x.com"),
    )))

    request = Request("https://api.x.com/1.1/guest/activate.json")
    resolver.cookiejar.add_cookie_header(request)

    sent = request.get_header("Cookie") or ""
    assert "auth_token=token-value" in sent
    assert "ct0=csrf-value" in sent


def test_a_session_is_not_sent_to_a_site_it_does_not_belong_to():
    from urllib.request import Request

    resolver = _Resolver()
    install_session(resolver, SiteSession.now("x.com", (
        SessionCookie("auth_token", "token-value", ".x.com"),
    )))

    elsewhere = Request("https://video.twimg.com/ext_tw_video/1/pu/vid/720x1280/x.mp4")
    resolver.cookiejar.add_cookie_header(elsewhere)

    assert elsewhere.get_header("Cookie") is None


def test_installing_a_session_logs_names_and_never_values(caplog):
    resolver = _Resolver()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        install_session(resolver, SiteSession.now("x.com", (
            SessionCookie("auth_token", "a-token-nobody-may-log", ".x.com"),
        )))

    assert "auth_token" in caplog.text
    assert "a-token-nobody-may-log" not in caplog.text


def test_the_resolver_is_never_asked_to_be_verbose_or_to_read_cookies():
    options = base_options()

    assert options["verbose"] is False
    assert options["cookiefile"] is None
    assert options["cookiesfrombrowser"] is None
    assert options["cachedir"] is False
    assert options["postprocessors"] == []
    assert options["writeinfojson"] is False
    assert options["logger"] is not None


def test_verbose_cannot_be_switched_on_through_the_overrides():
    """The debug build raises the application's log level, never the resolver's."""
    options = base_options(skip_download=True)

    assert options["verbose"] is False


def test_the_injected_logger_redacts_a_url_before_it_reaches_the_log(caplog):
    signed = (
        "https://rr2---sn-x.googlevideo.com/videoplayback"
        "?expire=1788668116&ip=2001-db8-SENTINEL&sig=SIG-SENTINEL-VALUE"
    )
    injected = base_options()["logger"]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        injected.debug(f'[debug] Invoking http downloader on "{signed}"')
        injected.warning(f"something about {signed}")
        injected.error(f"failed on {signed}")

    rendered = caplog.text
    assert "SIG-SENTINEL-VALUE" not in rendered
    assert "2001-db8-SENTINEL" not in rendered
    assert "1788668116" not in rendered
    assert "/videoplayback" in rendered, "the path is the diagnostic value"


def test_an_unsigned_url_survives_the_logger_intact(caplog):
    plain = "https://media.test/videoplayback/140.m4a"
    injected = base_options()["logger"]

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        injected.debug(f"fetching {plain}")

    assert plain in caplog.text


def test_every_provider_reaches_the_resolver_through_these_options():
    """One definition, or the guarantees above hold for one provider only.

    Every provider module is walked rather than a named list, because the
    failure worth guarding against is a *future* adapter quietly assembling its
    own option dictionary - which a list of today's adapters would never catch,
    and which no test of these options would ever see.

    Read from the source rather than from a call, because a module that never
    resolves anything during a test run still ships the code that would.
    """
    import ast
    import importlib
    import inspect
    import pkgutil

    import video_downloader.providers as package

    checked = 0
    for found in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{found.name}")
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "YoutubeDL"
            ):
                continue
            checked += 1
            argument = node.args[0] if node.args else None
            assert (
                isinstance(argument, ast.Call)
                and getattr(argument.func, "id", None) == "base_options"
            ), (
                f"{module.__name__} builds a YoutubeDL from something other "
                f"than a base_options() call"
            )

    assert checked, "no provider was checked; the walk found nothing"


# --- JavaScript runtimes ----------------------------------------------------


def test_every_runtime_this_yt_dlp_supports_is_enabled():
    """Enabling one does not choose it - yt-dlp still takes the best available.

    Listed from yt-dlp's own registry rather than spelled out here, so a release
    that adds a runtime does not silently leave it disabled.
    """
    from yt_dlp.globals import supported_js_runtimes

    enabled = base_options()["js_runtimes"]

    assert set(enabled) == set(supported_js_runtimes.value)
    assert "node" in enabled, "the runtime that is actually installed here"


def test_the_value_has_the_shape_yt_dlp_validates():
    """The CLI parser produces a list; the Python API wants a dict of dicts.

    Checked against `YoutubeDL` itself rather than against a reading of the
    documentation, because `_clean_js_runtimes` raises on the wrong shape and
    that is the contract that counts.
    """
    from yt_dlp import YoutubeDL

    with YoutubeDL(base_options()) as downloader:
        runtimes = downloader.params["js_runtimes"]

    assert isinstance(runtimes, dict)
    assert all(isinstance(config, dict) for config in runtimes.values())
    assert "node" in runtimes, "yt-dlp dropped it as unsupported"


def test_an_empty_registry_falls_back_to_the_default(monkeypatch):
    """Disabling everything would be worse than changing nothing."""
    from yt_dlp.globals import supported_js_runtimes

    monkeypatch.setattr(supported_js_runtimes, "value", {})

    assert enabled_js_runtimes() == {"deno": {}}


def test_the_runtimes_can_still_be_overridden():
    assert base_options(js_runtimes={"deno": {}})["js_runtimes"] == {"deno": {}}
