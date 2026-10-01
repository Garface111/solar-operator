"""RSS recycler: retire a grown process at a safe moment, never at a bad one.

The dangerous failure modes are the ones these pin: a worker exiting with no
supervisor to restart it, two web workers restarting at once, and a scheduler
restart landing on a daily send.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from api import rss_recycle as rr


def test_rss_is_readable_here():
    mb = rr.rss_mb()
    assert mb is None or mb > 1


def test_over_limit_needs_uptime_and_two_consecutive_strikes():
    g = rr._OverLimit(500, started=0)
    young = rr.MIN_UPTIME_S - 1
    assert not g.check(900, young)                       # just booted: never
    old = rr.MIN_UPTIME_S + 1
    assert not g.check(900, old)                         # first strike
    assert not g.check(400, old)                         # dipped: strikes reset
    assert not g.check(900, old)
    assert g.check(900, old)                             # sustained → recycle
    assert not rr._OverLimit(500, 0).check(None, old)    # unreadable RSS → no


def test_web_cooldown_lets_only_one_sibling_restart(tmp_path):
    stamp = str(tmp_path / "stamp")
    assert rr.claim_web_cooldown(stamp, at=1000.0)
    assert not rr.claim_web_cooldown(stamp, at=1000.0 + rr.WEB_COOLDOWN_S - 1)
    assert rr.claim_web_cooldown(stamp, at=1000.0 + rr.WEB_COOLDOWN_S + 1)


def test_recyclers_stay_off_unless_armed(monkeypatch):
    monkeypatch.delenv("WEB_WORKER_RECYCLE_MB", raising=False)
    assert rr.start_web_worker_recycler() is False
    monkeypatch.setenv("WORKER_RECYCLE_MB", "700")
    monkeypatch.delenv("SO_SUPERVISED", raising=False)
    # No restart loop around us → a self-exit would kill the scheduler.
    assert rr.start_scheduler_recycler(object()) is False


@pytest.fixture
def sched():
    s = BackgroundScheduler(timezone="UTC")
    s.start(paused=True)
    yield s
    if s.running:
        s.shutdown(wait=False)


def _noop():
    return None


def test_fast_interval_jobs_do_not_block_a_recycle(sched):
    sched.add_job(_noop, IntervalTrigger(minutes=1), id="poll")
    sched.add_job(_noop, CronTrigger(hour=9, minute=0), id="daily")
    now = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
    assert rr.scheduler_quiet(sched, now)


def test_a_daily_send_due_soon_blocks_the_recycle(sched):
    sched.add_job(_noop, CronTrigger(hour=9, minute=0, timezone="UTC"), id="daily")
    nrt = sched.get_job("daily").next_run_time
    assert rr.scheduler_quiet(sched, nrt - timedelta(minutes=30))
    assert not rr.scheduler_quiet(sched, nrt - timedelta(minutes=2))


def test_a_running_job_blocks_the_recycle(sched, monkeypatch):
    monkeypatch.setattr(rr, "_running_jobs", lambda s: 1)
    assert not rr.scheduler_quiet(sched)
    monkeypatch.setattr(rr, "_running_jobs", lambda s: None)   # unknown ⇒ not idle
    assert not rr.scheduler_quiet(sched)


def test_drain_exits_75_once_idle(sched, monkeypatch):
    exits = []
    monkeypatch.setattr(rr, "_running_jobs", lambda s: 0)
    assert rr.drain_and_exit(sched, exit_fn=exits.append, sleep=lambda _: None)
    assert exits == [rr.RECYCLE_EXIT_CODE]


def test_drain_gives_up_and_resumes_if_jobs_never_finish(sched, monkeypatch):
    exits = []
    monkeypatch.setattr(rr, "_running_jobs", lambda s: 1)
    assert not rr.drain_and_exit(sched, exit_fn=exits.append, sleep=lambda _: None,
                                 drain_timeout_s=0.01)
    assert exits == []
    assert sched.state == 1                                     # STATE_RUNNING again
