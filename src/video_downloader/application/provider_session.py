"""The provider resources one download job runs on.

`ProviderRegistry.resolve()` hands back a `Media` - a plain DTO whose sources
carry their own transport requirements (`MediaSource.headers`). The bytes are
fetched afterwards by a `BaseCore`, so a job needs two things: something that
turns a URL into a `Media`, and something that downloads an HLS `MediaSource`.

The two sides are deliberately independent transports. Providers scrape with
their own clients and may install whatever session state extraction needs; the
session's `core` is the download engine and stays provider-clean - what a media
request must send travels on the source and is applied per request. That is why
a direct `.m3u8` downloaded next to an xHamster job does not inherit xHamster's
`Referer`.

Scope: one job. `XHamsterAdapter` owns a `Client` -> `BaseCore` ->
`curl_cffi.AsyncSession`, and closing that is what `ProviderRegistry.close()`
does - which is why a single shared registry cannot be closed per job without
tearing the session out from under every other running job. Composition happens
in `bootstrap`; see the note there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from base_api.models import Media


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a job is started without a provider session factory.

    Its own type on purpose: "nobody wired the composition root" is a
    programming error, and must not read like an unsupported URL.
    """


@runtime_checkable
class MediaResolver(Protocol):
    """The registry side of a session: URL -> provider-neutral `Media`."""

    async def resolve(self, url: str) -> Media: ...


@runtime_checkable
class MediaDownloader(Protocol):
    """The engine side of a session: `DownloadConfigHLS` -> report or bool."""

    async def download(self, configuration: Any) -> Any: ...

    async def close(self) -> None: ...


@dataclass
class ProviderSession:
    """One job's provider resources, closed exactly once by whoever ran the job."""

    registry: MediaResolver
    core: MediaDownloader
    #: Per provider name (`Media.provider`), how a resolver that *downloads*
    #: that provider's media is prepared before it runs. A resolver track is
    #: fetched by re-resolving its page, and a page that only a signed-in viewer
    #: may see needs the same session for that second resolution as for the
    #: first. The download layer looks the preparation up by name and never
    #: learns which site it is for; a provider without one gets none.
    download_preparers: Mapping[str, Callable[[Any], None]] = field(default_factory=dict)
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        # Idempotent: a job's `finally` and an outer cleanup may both reach here,
        # and closing a curl session twice is not something to rely on.
        if self._closed:
            return
        self._closed = True

        registry_close = getattr(self.registry, "close", None)
        try:
            if registry_close is not None:
                # Providers own their extraction clients and close them here.
                # None of them holds the download core, so it is closed exactly
                # once - below, by the session that created it.
                await registry_close()
        finally:
            await self.core.close()
