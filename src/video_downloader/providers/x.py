"""X (formerly Twitter) provider adapter.

Much smaller than the YouTube one, and a measurement explains why. Resolving a
post on 2026-09-07 returns two kinds of format:

* `http-<bitrate>` - a progressive MP4 over plain https, one file carrying both
  picture and sound, published in four sizes. These are what this adapter
  offers, and there is nothing to pair or mux: X hands out finished files.
* `hls-*` - the same video as a segmented playlist, split into video-only
  renditions and separate audio-only ones. Filtered out rather than paired,
  because the progressive file is the same content without the assembly.

Three things the progressive formats do *not* state, all confirmed against the
live site, and each answered here by leaving a field unset rather than filling
it with a plausible value:

* **No codecs.** `vcodec` and `acodec` come back unset - yt-dlp has not opened
  the file and X does not say. `role` therefore stays unset too, which the
  selection layer already reads as "one finished file", and which is exactly
  what these are.
* **No quality label.** `format_note` is empty on every one of them, so the
  tier comes from the short side and `quality_label` stays `None` rather than a
  synthesised "1080p".
* **No size at all.** X states no `filesize`, and the `filesize_approx` yt-dlp
  derives from the bitrate is not a substitute: measured against the real
  `Content-Range` totals on 2026-09-07 it overstated all four formats of one
  post, by 2.52x, 2.69x, 2.81x and 5.70x. So `expected_size` stays unset, which
  avoids a large-download question asked about a number that is wrong by a
  factor of six. The real total is not lost with it: the download layer reads
  it off the one request for the last byte it makes anyway, to find out whether
  it can fetch the file itself - see `readable_total`.

Resolution goes through the same yt-dlp the YouTube adapter uses, on the same
terms - no cookies, no verbose, a redacting logger - because every reason for
those is about the resolver rather than about the site. It runs in a worker
thread: the resolution of one post was measured at 2.4 s, and `resolve` is
awaited on the thread that draws the window.

The one thing X does not state is why it will not hand a post over. A post the
site shows only to a signed-in viewer comes back as a tombstone with no reason
attached, and the resolver reports that as "no video could be found in this
tweet" - the same words it uses for a post of plain text. Those two are told
apart here rather than repeated; see `_refuse_withheld`.

A session, when the user has established one, is used for exactly one step: the
resolution. It is installed into the resolver's cookie jar for that call and
exists nowhere else - no cookie file, nothing on the media requests that follow.
That last part is deliberate rather than an omission: the progressive files X
publishes are ordinary CDN objects on `video.twimg.com`, and one was fetched to
the last byte with no cookie at all on 2026-09-07. Sending an account's session
to a CDN that does not ask for it would widen where that credential travels for
no gain.

Whether a session was in play also decides which refusal a withheld post gets.
Without one, a login is worth offering; with one, X has answered the signed-in
account and asking the user to sign in again would be asking them to repeat
what did not work.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from base_api.models import Media, MediaSource, MediaTrackInfo
from base_api.modules.errors import UnsupportedURLError

from video_downloader.application.provider_refusal import (
    ProviderLoginRequired,
    ProviderRefusal,
)
from video_downloader.application.track_download import YTDLP_TRANSPORT
from video_downloader.domain.site_session import SiteSession
from video_downloader.providers.ytdlp_options import base_options, install_session

logger = logging.getLogger(__name__)

#: Hosts this adapter claims. Both names are live - x.com is current and
#: twitter.com still redirects - so links of either age turn up in a paste box.
_HOSTS = frozenset({
    "x.com", "www.x.com", "mobile.x.com",
    "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com",
})

#: A post id is a snowflake: digits only. Matched by shape rather than by an
#: exact length, because the length grows with time and pinning today's would
#: quietly stop claiming valid links some years from now.
_POST_ID = re.compile(r"\A[0-9]{6,25}\Z")

#: What X appends when one attachment of a post is opened directly;
#: `/status/<id>/video/1` is the same post as `/status/<id>`.
_ATTACHMENT_SEGMENTS = ("video", "photo")

#: First path segments that name a feed rather than an account. Claimed anyway,
#: so the refusal can say what the link is instead of "unsupported URL".
_COLLECTION_PREFIXES = (
    "home", "explore", "search", "notifications", "messages", "settings",
    "compose", "hashtag", "bookmarks", "lists", "topics",
)

#: The status forms under X's internal `/i/` namespace.
_INTERNAL_POST_PATHS = (("i", "status"), ("i", "web", "status"))

#: What X calls its live formats, also under `/i/`. Named here so a pasted Space
#: link is refused as a Space: it is a live audio room, and the sentence a user
#: needs is that live is not supported, not that the link names no single post.
_LIVE_SEGMENTS = ("spaces", "broadcasts")

#: Said in two places - to a link that names a live format, and to a post that
#: turns out to be one - so the two cannot drift apart.
_LIVE_REFUSAL = "Live-Uebertragungen und Spaces werden nicht unterstuetzt."

#: What X answers when the session it was given is not one it accepts - expired,
#: revoked, or from an account that has been logged out elsewhere. Measured on
#: 2026-09-07 by resolving with a deliberately invalid `auth_token`:
#:
#:     Error(s) while querying API: Could not authenticate you
#:
#: It matters because X does not fall back on its own: with a session it rejects,
#: even a public post fails, where the same request without one would have been
#: answered. So this string is the signal to stop using the stored session.
_REJECTED_SESSION = ("could not authenticate you", "unauthorized", "401")

#: Protocols this application can hand to a downloader as one file. The `hls-*`
#: entries arrive as `m3u8_native` and are excluded by this.
_FETCHABLE_PROTOCOLS = frozenset({"https", "http"})

#: X wraps every link in a post as a t.co shortener, and yt-dlp composes the
#: title out of the post text, so a title very often ends in one. It has to go
#: before the title becomes a filename: the sanitiser keeps only the last path
#: component of what it is handed, so "Poster - https://t.co/abc" arrives on
#: disk as "abc.mp4" - a file named after a shortener token nobody can read.
_SHORTENED_LINK = re.compile(r"https?://t\.co/\S*")

#: Said to a post X refuses to describe at all. Measured on 2026-09-07 against a
#: post the site itself shows only to a signed-in viewer: the GraphQL answer is
#: `{"tweetResult": {"result": {"__typename": "TweetTombstone"}}}` and the
#: syndication endpoint says the same, neither of them stating a reason. So both
#: sentences name every reason it can be, because X named none of them - and
#: neither is "this post has no video", which is what the resolver reports for
#: it and what a user would act on by deleting the link.
#:
#: Which one is said depends on whether a session was in play, because that is
#: what decides whether there is anything left for the user to do.
_LOGIN_REFUSAL = (
    "X gibt diesen Beitrag ohne Anmeldung nicht heraus - moeglicherweise "
    "altersbeschraenkt, geschuetzt oder geloescht. Eine Anmeldung kann ihn "
    "freischalten."
)

_WITHHELD_REFUSAL = (
    "X gibt diesen Beitrag auch dem angemeldeten Konto nicht heraus - er ist "
    "geschuetzt, altersbeschraenkt oder geloescht."
)

#: The fields yt-dlp fills from X's own description of a post. A post X really
#: described states at least one of them; the tombstone above states none, and
#: that is the whole difference between "no video in this post" and "X said
#: nothing about this post". Matched on emptiness rather than on the tombstone
#: itself, because the tombstone never reaches this layer: the resolver turns
#: it into an ordinary answer with no formats and no facts.
_DESCRIBED_FIELDS = (
    "uploader", "uploader_id", "channel_id", "timestamp", "description", "duration",
)


class XError(Exception):
    """Base class for the adapter's own failures.

    The ones that are a refusal rather than a failure - X answered, and the
    answer was no - additionally carry `ProviderRefusal`, which is what lets the
    download service report them as the sentence they are instead of as a
    traceback.
    """


class XUnsupportedTargetError(XError, ProviderRefusal):
    """An X URL that names something other than one post.

    Its own type because "profiles are not supported" and "this link is not
    ours" are different sentences, and someone who pasted a profile deserves
    the first one.
    """


class XExtractionError(XError):
    """The post could not be resolved for a technical reason.

    Covers transport failures, an unreadable answer, and a post that no longer
    exists. Always raised `from` the original exception.
    """


class XUnavailableError(XError, ProviderRefusal):
    """X states this post may not be read without more than we have.

    A protected account, an age-restricted post, a suspended account, or a rate
    limit. Deliberately not an extraction error: nothing failed, and a retry
    returns the same answer. This application does not attempt to get past any
    of them.

    Some of them stop being true once the user signs in, and those are raised as
    `XLoginRequiredError` - the same refusal, plus the fact that there is
    something to do about it.
    """


class XLoginRequiredError(XUnavailableError, ProviderLoginRequired):
    """X would answer this for a signed-in viewer, and nobody is signed in.

    A subclass of the unavailable case rather than a sibling: it *is* one, and
    every caller that already treats "X will not hand this over" as a refusal
    keeps working. What it adds is that something can be done about it, which
    `ProviderLoginRequired` is the provider-neutral way to say - the three
    attributes below are what a login window needs, and it needs nothing else.

    `login_url` is X's own login page. This application never builds a login
    form: a password typed into one would be ours to hold, and X's captcha,
    two-factor and e-mail challenges only work on their own page anyway.

    The two cookie names are not a guess. `TwitterBaseIE.is_logged_in` is
    `bool(self._get_cookies(self._API_BASE).get('auth_token'))`, and the
    extractor sends `x-csrf-token` from `ct0`, so those two are precisely what
    "signed in" means to the resolver that will use them.
    """

    site = "x.com"
    login_url = "https://x.com/login"
    required_cookies = ("auth_token", "ct0")


class XLiveNotSupportedError(XError, ProviderRefusal):
    """A live broadcast or a Space, which this application does not download."""


class _SessionRejected(Exception):
    """X refused the session itself, rather than the post.

    Never leaves this module: `_extract` answers it by dropping the session and
    resolving again without one, so no caller ever has to know that the first
    attempt carried a credential X had already invalidated.
    """


class XNoSupportedSourceError(XExtractionError, ProviderRefusal):
    """The post was read, and carries no video.

    The ordinary case by far - a post with only text, a photo or a link
    preview - so it gets its own type and its own sentence rather than being
    reported as something having gone wrong.
    """


def _canonical_post_id(url: str) -> Optional[str]:
    """The numeric post id iff `url` names one post, else `None`.

    Pure parsing: no network, no side effects, the same answer every time.
    Raises rather than returning `None` for an X URL whose target this adapter
    recognises and declines - a profile, a feed, a Space - because those are
    ours to refuse with a reason rather than to silently disown.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "").lower() not in _HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]
    lowered = [segment.lower() for segment in segments]

    if not segments:
        raise XUnsupportedTargetError(
            "Das ist die Startseite von X, kein einzelner Beitrag - bitte den "
            "Link zu einem Beitrag angeben."
        )

    if lowered[0] == "i":
        for prefix in _INTERNAL_POST_PATHS:
            if tuple(lowered[:len(prefix)]) == prefix and len(segments) > len(prefix):
                return _post_id(segments[len(prefix)])
        if len(segments) > 1 and lowered[1] in _LIVE_SEGMENTS:
            # Refused here rather than after a resolution, because this is the
            # link someone actually pastes for a Space: `/i/spaces/<id>` names
            # one directly, and never resolves to a post that `_refuse_
            # unplayable` could recognise as live.
            raise XLiveNotSupportedError(_LIVE_REFUSAL)
        raise XUnsupportedTargetError(
            "Dieser X-Link zeigt auf keinen einzelnen Beitrag."
        )

    if lowered[0] in _COLLECTION_PREFIXES:
        raise XUnsupportedTargetError(
            "Feeds, Suchergebnisse und Listen werden nicht unterstuetzt - bitte "
            "den Link zu einem einzelnen Beitrag angeben."
        )

    # `/<user>/status/<id>`, the older `/<user>/statuses/<id>`, and either with
    # the `/video/1` suffix X adds when an attachment is opened directly.
    if len(segments) >= 3 and lowered[1] in ("status", "statuses"):
        if len(segments) > 3 and lowered[3] not in _ATTACHMENT_SEGMENTS:
            return None
        return _post_id(segments[2])

    if len(segments) <= 2:
        # `/<user>`, and `/<user>/likes`, `/<user>/with_replies` and friends.
        raise XUnsupportedTargetError(
            "Das ist ein Profil, kein einzelner Beitrag - bitte den Link zu "
            "einem Beitrag angeben."
        )

    return None


def _post_id(candidate: str) -> Optional[str]:
    return candidate if _POST_ID.match(candidate) else None


def _stated_int(value: Any) -> Optional[int]:
    """The integer the resolver stated, or `None` when it stated none."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _codec(value: Any) -> Optional[str]:
    """A codec string, or `None` for "the resolver named none".

    X names none on every progressive format, so this returns `None` in
    practice. It stays because the alternative - deriving a codec from the
    `.mp4` extension - would be this adapter stating a fact X did not.
    """
    if not isinstance(value, str) or not value or value == "none":
        return None
    return value


def _rejects_session(message: str) -> bool:
    """Whether X refused the session itself rather than the post.

    Its own function because the consequence is unusual: this is the one
    resolver failure this adapter answers by changing what it holds - dropping
    the session - instead of reporting it. Matched on X's own wording, which is
    the only contract available, and kept narrow: a message that merely mentions
    a login is not this, and the fallback is a refusal the user can act on.
    """
    lowered = message.lower()
    return any(phrase in lowered for phrase in _REJECTED_SESSION)


def _fetchable(entry: Any) -> bool:
    """Whether this entry is one file this application can hand to a downloader.

    Dimensions are the test rather than codecs: X states no codecs at all on the
    formats that *are* fetchable, and every one of them states a width and a
    height. The audio-only renditions have neither, and are excluded by the
    protocol check before this even matters.
    """
    if not isinstance(entry, dict) or not entry.get("url"):
        return False
    if entry.get("protocol") not in _FETCHABLE_PROTOCOLS:
        return False
    return bool(_stated_int(entry.get("width")) and _stated_int(entry.get("height")))


class XAdapter:
    """Resolves an X post URL into a provider-neutral `Media`.

    Holds no client and no session of its own: `yt_dlp.YoutubeDL` is constructed
    per call and closed with it, so nothing accumulated during one resolution can
    reach the next. A *site* session, if the user established one, is read fresh
    per resolution through `session_source` - which is what lets a job that just
    prompted for a login retry and find it, without this adapter caching a
    credential for the life of the process.
    """

    def __init__(
        self,
        resolver: Any = None,
        session_source: Any = None,
        forget_session: Any = None,
    ) -> None:
        # Injectable so the tests can drive stored, redacted fixtures without a
        # network and without patching a module global.
        self._resolver = resolver
        # A callable returning the stored `SiteSession` for x.com, or `None`.
        # A callable rather than the session itself: it is asked at resolution
        # time, so a login that happened one second ago is already in effect.
        self._session_source = session_source
        # Called when X rejects the stored session, so the next resolution does
        # not carry a credential the site has already refused. Separate from the
        # source because reading and discarding are different rights: this
        # adapter may drop its own site's session and nothing else.
        self._forget_session = forget_session

    def supports(self, url: str) -> bool:
        """Whether this adapter claims `url`. Cheap, synchronous, network-free.

        Every refusal the parser can already name is caught, not just one kind.
        A profile or a Space URL is ours: claiming it is what lets `resolve` say
        why it is declined instead of the registry saying "unsupported". Catching
        the base class also keeps this method total, which it has to be - the
        registry calls it for every adapter on every URL a user pastes, so an
        exception escaping here would fail links belonging to somebody else.
        """
        try:
            return _canonical_post_id(url) is not None
        except XError:
            return True

    def _session(self) -> SiteSession | None:
        """The session to resolve with, or `None` for "nobody is signed in".

        Every failure answers `None`, including a store that cannot be read: a
        resolution that would have worked anonymously must not be lost because a
        session file was unreadable, and the refusal the user then sees offers
        the login again.

        A session missing either cookie is not one. Without `auth_token` X
        answers exactly as it does to a stranger, so treating a half session as
        signed in would replace an offer to log in with "your account may not
        see this" - the one sentence that is certainly wrong.
        """
        if self._session_source is None:
            return None
        try:
            session = self._session_source()
        except Exception as error:  # noqa: BLE001 - a store may never fail a resolution
            logger.warning("Gespeicherte X-Anmeldung ist nicht nutzbar: %s", error)
            return None

        if session is None:
            return None
        if not session.has_all(XLoginRequiredError.required_cookies):
            logger.info(
                "Gespeicherte X-Anmeldung ist unvollstaendig (%s) und wird nicht "
                "verwendet.", ", ".join(session.names()),
            )
            return None
        return session

    async def resolve(self, url: str) -> Media:
        """Resolve a post URL into `Media` with every fetchable file."""
        post_id = _canonical_post_id(url)
        if post_id is None:
            raise UnsupportedURLError(f"Not a supported X post URL: {url}")

        # yt-dlp is synchronous and `resolve` is awaited on the GUI's event
        # loop thread: measured on 2026-09-07 one post cost 2.4 s there - a
        # guest token, a GraphQL call, and on the first resolution of a process
        # the import of yt_dlp itself - and the watchdog reported the UI frozen
        # for every one of those seconds. The fetch has run in a worker thread
        # for exactly this reason since it was written; resolution is no
        # different, and it is the half a user waits on first.
        # The session comes back from the resolution rather than being read
        # here, because the resolution is what knows which one was used: a
        # session X rejects is dropped mid-flight and the retry is anonymous, so
        # reading the store before or after would both describe a different
        # attempt than the one that produced `info`. Reading it there also keeps
        # a file read and a decryption off the thread that draws the window.
        info, session = await asyncio.to_thread(self._extract, url)
        if not isinstance(info, dict):
            # Checked here rather than in `_extract`, so it holds for an
            # injected resolver too: nothing below may assume a shape.
            raise XExtractionError(
                f"The resolver returned {type(info).__name__}, not a post description"
            )

        self._refuse_unplayable(info)

        formats = [entry for entry in (info.get("formats") or []) if _fetchable(entry)]
        if not formats:
            # Asked only here. With a format in hand it makes no difference what
            # X said about the post; without one it is the whole difference
            # between the two sentences.
            self._refuse_withheld(info, logged_in=session is not None)
            raise XNoSupportedSourceError("Dieser Beitrag enthaelt kein Video.")

        sources = [
            source
            for source in (self._source_from_format(e, post_id) for e in formats)
            if source is not None
        ]
        if not sources:
            raise XNoSupportedSourceError(
                f"None of the {len(formats)} formats X offers for {post_id} is usable"
            )

        return Media(
            provider="x",
            original_url=url,
            # X posts have no title. yt-dlp composes one from the poster's name
            # and the post text; it is kept as given, because deciding what a
            # post is "called" is not this adapter's judgement to make.
            title=self._title(info, post_id),
            provider_id=post_id,
            authors=[info["uploader"]] if isinstance(info.get("uploader"), str) else [],
            thumbnail=info.get("thumbnail") if isinstance(info.get("thumbnail"), str) else None,
            # Stated in seconds and fractional - 12.422 for the post measured.
            duration=_stated_int(info.get("duration")),
            sources=sources,
        )

    @staticmethod
    def _title(info: dict, post_id: str) -> str:
        """What to call a post, which X itself does not name.

        yt-dlp composes "<poster> - <post text>", and the post text is often
        nothing but a shortened link. Those are stripped, and what remains is
        used only if it says more than the poster's name already does -
        otherwise the post id joins it, so two posts by one account do not
        arrive on disk under the same name.
        """
        uploader = info.get("uploader")
        uploader = uploader.strip() if isinstance(uploader, str) else ""

        title = info.get("title")
        if isinstance(title, str):
            spelled_out = _SHORTENED_LINK.sub("", title)
            spelled_out = re.sub(r"\s+", " ", spelled_out).strip(" -–—")
            if spelled_out and spelled_out != uploader:
                return spelled_out

        return f"{uploader} - {post_id}" if uploader else post_id

    def _extract(self, url: str) -> tuple[dict, SiteSession | None]:
        """The post, and the session it was actually resolved with.

        Both, because the caller decides a sentence by it: a session X will not
        accept is dropped here and the resolution runs again without one, so
        what was in effect at the end is not what the store said at the start.

        Dropping it is not optional. With a rejected session X refuses even a
        public post - measured on 2026-09-07: an invalid `auth_token` turned a
        post that resolves anonymously into "Could not authenticate you" - so an
        expired login would break every X download until the user worked out
        that signing out is the cure. The second attempt carries nothing, which
        is what makes a third impossible.
        """
        session = self._session()
        try:
            return self._attempt(url, session), session
        except _SessionRejected:
            logger.info(
                "X hat die gespeicherte Anmeldung abgelehnt; sie wird verworfen "
                "und der Beitrag ohne sie aufgeloest."
            )
            self._discard_session()
            return self._attempt(url, None), None

    def _attempt(self, url: str, session: SiteSession | None) -> dict:
        """One resolution, with every failure mapped onto this adapter's names."""
        from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError

        try:
            return self._read(url, session)
        except GeoRestrictedError as error:
            raise XUnavailableError("In dieser Region nicht verfuegbar.") from error
        except (DownloadError, ExtractorError) as error:
            if session is not None and _rejects_session(str(error)):
                raise _SessionRejected(type(error).__name__) from error
            raise self._classify(
                str(error), error, logged_in=session is not None
            ) from error
        except OSError as error:
            raise XExtractionError(f"Request to X failed: {error}") from error

    def _discard_session(self) -> None:
        """Forget the session X refused. Never the reason a resolution fails."""
        if self._forget_session is None:
            return
        try:
            self._forget_session()
        except Exception as error:  # noqa: BLE001 - housekeeping, not the job
            logger.warning("Abgelehnte X-Anmeldung konnte nicht entfernt werden: %s", error)

    def _read(self, url: str, session: SiteSession | None) -> Any:
        """The resolution itself, with the resolver's own failures untouched."""
        if self._resolver is not None:
            return self._resolver(url)

        from yt_dlp import YoutubeDL

        with YoutubeDL(
            base_options(
                skip_download=True,
                # A post with no format is an answer to be read, not a failure:
                # only what X said *besides* the formats tells a text-or-photo
                # post apart from one X withheld entirely, and the exception
                # this suppresses carries none of it. Every other failure still
                # raises, and still goes through `_classify`.
                ignore_no_formats_error=True,
            )
        ) as resolver:
            if session is not None:
                # Into this resolver's jar and no further: the jar dies with the
                # `with` block, and nothing writes it anywhere.
                install_session(resolver, session)
            return resolver.extract_info(url, download=False)

    @staticmethod
    def _classify(message: str, error: Exception, logged_in: bool = False) -> XError:
        """Turn one resolver message into the failure this application names.

        Matched on the text because that is what the resolver gives us: it
        reports X's own wording, and those strings are the contract we actually
        have. Anything unrecognised stays an extraction error rather than being
        guessed into a friendlier one.

        `logged_in` decides nothing about *what* happened and everything about
        what is left to do. Three of these answers - age restriction, a
        protected account, a demand to log in - stop being true for a signed-in
        viewer, so without a session they are raised as the refusal that offers
        one. A suspended account and a rate limit answer the same to everybody
        and are never one of those.

        Defaults to `False` because that is the state this application is in
        until the user changes it.
        """
        def needs_account(anonymous: str, signed_in: str) -> XError:
            """One fact, said to someone who can act on it and to someone who cannot."""
            if logged_in:
                return XUnavailableError(signed_in)
            return XLoginRequiredError(anonymous)

        lowered = message.lower()
        if "no video could be found" in lowered or "no media" in lowered:
            return XNoSupportedSourceError("Dieser Beitrag enthaelt kein Video.")
        if "nsfw" in lowered or ("age" in lowered and "restrict" in lowered):
            return needs_account(
                "Altersbeschraenkter Beitrag - eine Anmeldung kann ihn freischalten.",
                "Altersbeschraenkter Beitrag - das angemeldete Konto darf ihn nicht sehen.",
            )
        if "protected" in lowered or "private" in lowered:
            return needs_account(
                "Dieses Konto ist geschuetzt - als Follower angemeldet ist der "
                "Beitrag lesbar.",
                "Dieses Konto ist geschuetzt und folgt dem angemeldeten Konto nicht.",
            )
        if "suspended" in lowered:
            return XUnavailableError("Dieses Konto ist gesperrt.")
        if "rate limit" in lowered or "too many requests" in lowered:
            return XUnavailableError(
                "X hat die Anfrage vorerst abgelehnt - bitte spaeter erneut versuchen."
            )
        if "log in" in lowered or "login" in lowered or "authenticat" in lowered:
            return needs_account(
                "X verlangt fuer diesen Beitrag eine Anmeldung.",
                "X akzeptiert die gespeicherte Anmeldung fuer diesen Beitrag nicht.",
            )
        if "not found" in lowered or "unavailable" in lowered or "deleted" in lowered:
            return XExtractionError("Beitrag nicht gefunden oder geloescht.")
        return XExtractionError(f"X could not be resolved: {error}")

    @staticmethod
    def _refuse_withheld(info: dict, logged_in: bool = False) -> None:
        """Refuse a post X declined to describe, as that rather than as empty.

        Decided from the absence of every field X would have stated, not from
        the tombstone that caused it: the tombstone never reaches this layer -
        the resolver has already turned it into an ordinary answer with no
        formats and no facts - and reading it from the answer's own shape is
        what makes this hold for an injected resolver too.

        `logged_in` picks the sentence, and with it whether a login is offered.
        X states no reason either way, so neither sentence claims one.
        """
        if any(info.get(field) for field in _DESCRIBED_FIELDS):
            return
        if logged_in:
            raise XUnavailableError(_WITHHELD_REFUSAL)
        raise XLoginRequiredError(_LOGIN_REFUSAL)

    @staticmethod
    def _refuse_unplayable(info: dict) -> None:
        """Live broadcasts and Spaces are refused before any format is read."""
        if info.get("is_live") or info.get("live_status") in (
            "is_live", "is_upcoming", "post_live"
        ):
            raise XLiveNotSupportedError(_LIVE_REFUSAL)

    @staticmethod
    def _source_from_format(entry: dict, post_id: str) -> Optional[MediaSource]:
        width = _stated_int(entry.get("width"))
        height = _stated_int(entry.get("height"))
        if not width or not height:
            return None

        format_id = str(entry.get("format_id") or "")
        bitrate = entry.get("tbr")
        headers = entry.get("http_headers")

        return MediaSource(
            url=entry["url"],
            # Fetched by the resolver that produced the URL rather than by the
            # engine's transport. The download layer probes each source once and
            # moves it onto the engine when it can read the whole file - which
            # for X it can: these are ordinary CDN objects.
            source_type=YTDLP_TRANSPORT,
            headers=dict(headers) if isinstance(headers, dict) else {},
            # Only a size X actually stated, which for these formats means
            # none. `filesize_approx` is bitrate times duration, and measured
            # against the real totals it was 2.5x to 5.7x too high - enough to
            # ask about a two-gigabyte download that is four hundred megabytes,
            # and enough to make the readability probe ask for a byte past the
            # end of the file and conclude the file is unreadable.
            expected_size=_stated_int(entry.get("filesize")),
            # The short side. X publishes no label of its own, and this is the
            # only tier that survives the portrait videos X is full of.
            quality_value=min(width, height),
            quality_label=None,
            # Stable across a re-resolution, which the URL is not.
            identity=f"x:{post_id}:{format_id}" if format_id else None,
            track=MediaTrackInfo(
                # Left unset deliberately: X states no codecs, so nothing here
                # knows whether the file carries sound. A source that states no
                # role is read as one finished file, which is what it is.
                role=None,
                container=entry.get("ext") if isinstance(entry.get("ext"), str) else None,
                video_codec=_codec(entry.get("vcodec")),
                audio_codec=_codec(entry.get("acodec")),
                bitrate_bps=int(bitrate * 1000)
                if isinstance(bitrate, (int, float)) and bitrate > 0
                else None,
                width=width,
                height=height,
            ),
        )
