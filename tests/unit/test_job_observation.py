"""Wer einem Job zusieht, und auf welchem Thread er dabei steht.

Drei Zusicherungen, die es vorher nicht gab und die zusammen den Grund bilden,
warum die Oberflaeche austauschbar wird:

* **Mehrere Beobachter.** Solange `on_change` ein einzelnes Feld war, machte
  ein zweiter Beobachter den ersten stumm - ohne Fehler, ohne Warnung.
* **Abmelden.** Ein Eintrag, der aus der Liste genommen wurde, darf nichts mehr
  bekommen. Vorher blieb er haengen.
* **Ein Thread.** Fortschritt erreicht einen Job aus Worker-Threads; bisher hat
  die Qt-Signalverbindung das aufgefangen, weil sie ueber Threadgrenzen hinweg
  zu einer Queued Connection wird. Das ist eine Eigenschaft der Oberflaeche.
  Jetzt macht es der Kern selbst.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from video_downloader.domain.download_job import DownloadJob, LifecycleState


def a_job(tmp_path: Path) -> DownloadJob:
    return DownloadJob(url="https://provider.test/x", quality="best", output_dir=tmp_path)


# --- mehrere Beobachter ----------------------------------------------------


def test_two_listeners_both_hear_every_change(tmp_path):
    """Der Fall, den das einzelne Feld verschluckt hat."""
    job = a_job(tmp_path)
    first: list[LifecycleState] = []
    second: list[LifecycleState] = []

    job.add_listener(lambda changed: first.append(changed.state))
    job.add_listener(lambda changed: second.append(changed.state))

    job.transition(LifecycleState.QUEUED)
    job.transition(LifecycleState.DOWNLOADING)

    assert first == [LifecycleState.QUEUED, LifecycleState.DOWNLOADING]
    assert second == first, "der zweite Beobachter hat den ersten verdraengt"


def test_registering_the_same_listener_twice_does_not_double_it(tmp_path):
    job = a_job(tmp_path)
    seen: list[LifecycleState] = []

    def listener(changed: DownloadJob) -> None:
        seen.append(changed.state)

    job.add_listener(listener)
    job.add_listener(listener)
    job.transition(LifecycleState.QUEUED)

    assert seen == [LifecycleState.QUEUED]
    assert job.listener_count == 1


def test_a_removed_listener_hears_nothing_more(tmp_path):
    """Der Beobachter-Ueberhang, den ein entfernter Listeneintrag hinterliess."""
    job = a_job(tmp_path)
    seen: list[LifecycleState] = []

    def listener(changed: DownloadJob) -> None:
        seen.append(changed.state)

    job.add_listener(listener)
    job.transition(LifecycleState.QUEUED)

    job.remove_listener(listener)
    job.transition(LifecycleState.DOWNLOADING)
    job.transition(LifecycleState.COMPLETED)

    assert seen == [LifecycleState.QUEUED]
    assert job.listener_count == 0


def test_removing_a_listener_that_was_never_added_is_not_an_error(tmp_path):
    """Abgemeldet wird auf dem Aufraeumpfad, und der darf nicht werfen."""
    job = a_job(tmp_path)
    job.remove_listener(lambda changed: None)  # kein Fehler


def test_a_listener_may_remove_itself_while_being_notified(tmp_path):
    """Genau das tut ein Listeneintrag, der sich beim letzten Zustand abmeldet."""
    job = a_job(tmp_path)
    seen: list[LifecycleState] = []

    def once(changed: DownloadJob) -> None:
        seen.append(changed.state)
        job.remove_listener(once)

    job.add_listener(once)
    job.transition(LifecycleState.QUEUED)
    job.transition(LifecycleState.DOWNLOADING)

    assert seen == [LifecycleState.QUEUED]


def test_a_listener_that_raises_does_not_take_the_others_with_it(tmp_path):
    """Ein Fehler in einer Oberflaeche ist ihr Fehler, nicht der des Downloads."""
    job = a_job(tmp_path)
    seen: list[LifecycleState] = []

    def broken(changed: DownloadJob) -> None:
        raise RuntimeError("die Oberflaeche ist kaputt")

    job.add_listener(broken)
    job.add_listener(lambda changed: seen.append(changed.state))

    job.transition(LifecycleState.QUEUED)  # darf nicht werfen

    assert seen == [LifecycleState.QUEUED]
    assert job.state == LifecycleState.QUEUED, "der Zustand ist trotzdem gesetzt"


def test_every_change_advances_the_sequence_number(tmp_path):
    """Die Reihenfolge zweier Beobachtungen ist daran vergleichbar."""
    job = a_job(tmp_path)
    marks: list[int] = []
    job.add_listener(lambda changed: marks.append(changed.seq))

    job.transition(LifecycleState.QUEUED)
    job.update_progress(1, 10)
    job.transition(LifecycleState.DOWNLOADING)

    assert marks == sorted(marks) and len(set(marks)) == len(marks)


# --- ein Thread ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_worker_thread_update_reaches_the_listener_on_the_loop(tmp_path):
    """Der Fall, den bisher nur Qt richtig gemacht hat.

    yt-dlp meldet seinen Fortschritt aus `asyncio.to_thread`, das Muxen ebenso,
    und der Remux der Engine auch. Ein Beobachter darf davon nichts merken.
    """
    job = a_job(tmp_path)
    job.bind_loop()
    loop_thread = threading.get_ident()

    seen_on: list[int] = []
    applied = asyncio.Event()

    def listener(changed: DownloadJob) -> None:
        seen_on.append(threading.get_ident())
        applied.set()

    job.add_listener(listener)

    await asyncio.to_thread(job.update_progress, 5, 10)
    await asyncio.wait_for(applied.wait(), timeout=2)

    assert seen_on, "aus dem Worker-Thread kam keine Benachrichtigung an"
    assert all(ident == loop_thread for ident in seen_on)
    assert job.downloaded_segments == 5 and job.total_segments == 10


@pytest.mark.asyncio
async def test_a_loop_thread_change_does_not_overtake_queued_worker_changes(tmp_path):
    """Neuere Ereignisse duerfen aeltere nicht ueberholen.

    Ohne die Warteschlange stuende `COMPLETED` beim Beobachter vor dem letzten
    Fortschritt - die Oberflaeche saehe einen fertigen Job und danach wieder
    einen laufenden.
    """
    job = a_job(tmp_path)
    job.bind_loop()

    seen: list[tuple[int, LifecycleState]] = []
    job.add_listener(lambda changed: seen.append((changed.downloaded_segments, changed.state)))

    enqueued = threading.Event()

    def worker() -> None:
        for step in range(1, 21):
            job.update_progress(step, 100)
        enqueued.set()

    thread = threading.Thread(target=worker)
    thread.start()
    enqueued.wait(timeout=2)
    thread.join(timeout=2)

    # Noch auf dem Loop-Thread, waehrend zwanzig Meldungen anstehen.
    job.mark_completed()
    await asyncio.sleep(0.05)

    assert seen, "nichts kam an"
    progress = [done for done, _ in seen]
    assert progress == sorted(progress), f"Fortschritt lief rueckwaerts: {progress}"
    assert seen[-1][1] == LifecycleState.COMPLETED
    assert seen[-1][0] == 20, "der Abschluss hat den letzten Fortschritt ueberholt"


@pytest.mark.asyncio
async def test_a_stop_is_immediate_and_does_not_wait_for_the_loop(tmp_path):
    """Ein Abbruch, der auf den naechsten Loop-Durchlauf wartet, laedt weiter."""
    job = a_job(tmp_path)
    job.bind_loop()

    await asyncio.to_thread(job.request_stop)

    assert job.stop_event.is_set()


def test_a_job_without_a_bound_loop_still_notifies(tmp_path):
    """Ein Einheitstest baut keinen Loop - und bekommt trotzdem Ereignisse."""
    job = a_job(tmp_path)
    seen: list[LifecycleState] = []
    job.add_listener(lambda changed: seen.append(changed.state))

    job.transition(LifecycleState.QUEUED)

    assert seen == [LifecycleState.QUEUED]


@pytest.mark.asyncio
async def test_a_listener_may_change_the_job_without_deadlocking(tmp_path):
    """Die Sperre schuetzt den Zaehler, nicht die Benachrichtigung.

    Laege sie ueber `_notify()`, stuende ein Beobachter, der den Job dabei
    anfasst, auf einer nicht wiedereintrittsfaehigen Sperre still - und mit ihm
    der Loop.
    """
    job = a_job(tmp_path)
    job.bind_loop()
    seen: list[str] = []

    def reacting(changed: DownloadJob) -> None:
        seen.append(changed.title)
        if changed.state is LifecycleState.DOWNLOADING and not changed.title:
            changed.title = "unterwegs"
            changed.transition(LifecycleState.MUXING)

    job.add_listener(reacting)

    job.transition(LifecycleState.DOWNLOADING)

    assert job.state is LifecycleState.MUXING
    assert job.title == "unterwegs"
