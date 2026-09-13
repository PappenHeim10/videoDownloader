"""Was ein Fortschrittswert behaupten darf, und was ein fertiger Job sagen muss.

Zwei Zusicherungen, die vorher keine waren:

* **Ein abgeschlossener Job steht nie unter 100 %.** Der Fall, der das brach:
  ein Resolver-Track, dessen Provider keine Groesse nennt und fuer den yt-dlp
  weder `total_bytes` noch `total_bytes_estimate` liefert. Dann blieb
  `total_segments` null, `_handle_download_result` korrigierte den Fortschritt
  nur `if job.total_segments`, und ein fertiger Download stand auf 0 %.
* **Ein Fortschritt ohne Nenner sagt das, statt einen zu erfinden.** Solange
  eine Phase ihre Groesse nicht kennt, ist der Gesamtwert null - was
  `has_known_total` als "unbekannt" liest und die Oberflaeche als
  unbestimmten Balken zeichnet. Der Prozentsatz erscheint erst, wenn er
  stimmen kann, und faellt danach nicht mehr zurueck.

Beide laufen durch `run_download_job`, weil beide Aussagen ueber den fertigen
Job sind und nicht ueber eine einzelne Funktion.
"""

from __future__ import annotations

import pytest
from base_api.models import Media, MediaSource, MediaTrackInfo

from video_downloader.application import track_download
from video_downloader.application.download_service import run_download_job
from video_downloader.application.provider_session import ProviderSession
from video_downloader.application.track_download import _AggregateProgress, _Phase
from video_downloader.domain.download_job import DownloadJob, LifecycleState

WATCH_URL = "https://provider.test/watch?v=abc"


def unsized_media(roles: tuple[str, ...] = ("video",)) -> Media:
    """Ein Medium, dessen Quellen keine Groesse nennen - wie X es ausliefert."""
    media = Media(provider="test", original_url=WATCH_URL, title="Unsized Video")
    media.sources = [
        MediaSource(
            url=f"http://provider.test/{role}.mp4",
            source_type=track_download.YTDLP_TRANSPORT,
            quality_value=1080 if role == "video" else None,
            quality_label="1080p" if role == "video" else None,
            expected_size=None,
            identity=f"test:abc:{role}",
            track=MediaTrackInfo(
                role=role,
                container="mp4",
                video_codec="avc1.640028" if role == "video" else None,
                audio_codec="mp4a.40.2" if role == "audio" else None,
            ),
        )
        for role in roles
    ]
    return media


class _UnreachableCore:
    async def download(self, configuration):
        raise AssertionError("der Engine-Pfad darf hier nicht erreicht werden")

    async def close(self) -> None:
        return None


def session_for(media: Media) -> ProviderSession:
    class Registry:
        async def resolve(self, url: str) -> Media:
            return media

        async def close(self) -> None:
            return None

    return ProviderSession(registry=Registry(), core=_UnreachableCore())


@pytest.fixture
def resolver_without_totals(monkeypatch):
    """Der Resolver-Pfad, so wie er sich ohne jede Groessenangabe verhaelt."""

    async def no_total(source, timeout: float = 15.0):
        return None

    monkeypatch.setattr(track_download, "readable_total", no_total)

    async def fake_resolver(source, target, callback, stop_event, page_url):
        target.write_bytes(b"x" * 4096)
        # Bytes ja, Gesamtwert nein - genau das, was yt-dlp hier meldet.
        callback(4096, 0)

    monkeypatch.setattr(track_download, "_download_via_resolver", fake_resolver)


# --- ein fertiger Job ist fertig -------------------------------------------


@pytest.mark.asyncio
async def test_a_completed_job_never_reports_less_than_full(
    tmp_path, resolver_without_totals
):
    """Fertig ist fertig, auch wenn nie jemand die Groesse genannt hat."""
    job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)

    await run_download_job(job, session_factory=lambda: session_for(unsized_media()))

    assert job.state == LifecycleState.COMPLETED, job.error
    assert job.progress == 100.0
    # Und der Balken darf nicht weiter "unbekannt" behaupten, sonst zeichnet
    # die Oberflaeche einen laufenden Vorgang fuer eine fertige Datei.
    assert job.has_known_total


@pytest.mark.asyncio
async def test_a_completed_job_that_transferred_nothing_is_still_complete(tmp_path, monkeypatch):
    """Auch ohne einen einzigen gemeldeten Byte-Wert steht der Job auf 100 %."""

    async def no_total(source, timeout: float = 15.0):
        return None

    monkeypatch.setattr(track_download, "readable_total", no_total)

    async def silent_resolver(source, target, callback, stop_event, page_url):
        target.write_bytes(b"x" * 16)
        # Kein einziger Callback - der Downloader meldet gar nichts.

    monkeypatch.setattr(track_download, "_download_via_resolver", silent_resolver)

    job = DownloadJob(url=WATCH_URL, quality="best", output_dir=tmp_path)
    await run_download_job(job, session_factory=lambda: session_for(unsized_media()))

    assert job.state == LifecycleState.COMPLETED, job.error
    assert job.progress == 100.0


# --- ein Nenner, den niemand kennt, wird nicht erfunden ---------------------


def test_an_unknown_phase_size_keeps_the_total_unknown():
    """Solange eine Phase ihre Groesse nicht kennt, gibt es keinen Prozentsatz.

    Vorher wurde der Gesamtwert aus den bereits bekannten Phasen gebildet. Der
    Balken sprang dadurch auf 100 %, sobald die erste Spur fertig war, und fiel
    zurueck, sobald die zweite ihre Groesse lernte.
    """
    reported: list[tuple[int, int]] = []
    phases = [_Phase(name="video", weight=0), _Phase(name="audio", weight=0)]
    progress = _AggregateProgress(phases, lambda done, total: reported.append((done, total)))

    video = progress.callback_for("video")
    video(100, 100)          # die Videospur lernt ihre Groesse
    progress.complete("video")

    # Die Audiospur ist noch ungewogen - ein Gesamtwert waere hier eine Luege.
    assert reported[-1][1] == 0, "Gesamtwert behauptet, obwohl eine Phase ungewogen ist"

    audio = progress.callback_for("audio")
    audio(10, 20)            # jetzt kennt auch sie ihre Groesse
    assert reported[-1] == (110, 120)


def test_the_reported_percentage_never_falls_back():
    """Sobald ein Prozentsatz erscheint, waechst er nur noch."""
    reported: list[tuple[int, int]] = []
    phases = [_Phase(name="video", weight=0), _Phase(name="audio", weight=0)]
    progress = _AggregateProgress(phases, lambda done, total: reported.append((done, total)))

    progress.callback_for("video")(100, 100)
    progress.complete("video")
    progress.callback_for("audio")(5, 20)
    progress.callback_for("audio")(20, 20)
    progress.complete("audio")

    percentages = [done / total * 100 for done, total in reported if total]
    assert percentages == sorted(percentages), f"Fortschritt lief rueckwaerts: {percentages}"
