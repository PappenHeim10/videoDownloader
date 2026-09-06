"""Provider adapters that live in this application rather than in a fork.

`base_api` ships the provider-neutral contract (`MediaProvider`, `Media`,
`MediaSource`) and the direct-URL adapter; `xhamster_api` ships its own. An
adapter that belongs to no upstream project belongs here, where it can be
changed without moving a pin.
"""

from video_downloader.providers.peertube import (
    PeerTubeAdapter,
    PeerTubeDownloadDisabledError,
    PeerTubeError,
    PeerTubeExtractionError,
)
from video_downloader.providers.youtube import (
    YouTubeAdapter,
    YouTubeError,
    YouTubeExtractionError,
    YouTubeLiveNotSupportedError,
    YouTubeNoSupportedSourceError,
    YouTubePlayerContractError,
    YouTubeUnavailableError,
    YouTubeUnsupportedTargetError,
)

__all__ = [
    "PeerTubeAdapter",
    "PeerTubeDownloadDisabledError",
    "PeerTubeError",
    "PeerTubeExtractionError",
    "YouTubeAdapter",
    "YouTubeError",
    "YouTubeExtractionError",
    "YouTubeLiveNotSupportedError",
    "YouTubeNoSupportedSourceError",
    "YouTubePlayerContractError",
    "YouTubeUnavailableError",
    "YouTubeUnsupportedTargetError",
]
