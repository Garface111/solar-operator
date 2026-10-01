"""Recycle a long-lived process before its heap grows into a Railway bill.

Railway bills RAM by the minute, and our Python processes only grow: web workers
drifted 0.3 GB → 2 GB each between deploys and the scheduler worker 0.4 → 2.2 GB
(prod metrics, Sep 2026; MALLOC_ARENA_MAX=2 already set, so this is heap, not
arena fragmentation). RAM was $88 of the $104 September invoice. Restarting a
process at a quiet moment drops it back to its boot footprint; nothing is lost
because every bit of durable state is in Postgres.

Two shapes, both OFF unless their env var is set (start.sh sets them only where a
supervisor will bring the process back — a self-exit with nobody to restart it
would be an outage, not a saving):

* web (WEB_WORKER_RECYCLE_MB): uvicorn --workers N; the uvicorn supervisor
  restarts any worker that exits. An over-limit worker sends itself SIGTERM, which
  uvicorn handles gracefully (stop accepting, finish in-flight requests within
  --timeout-graceful-shutdown). A /tmp cooldown stamp, shared by the workers of one
  container, keeps two of them from restarting at once, so the other keeps serving.

* scheduler worker (WORKER_RECYCLE_MB): start.sh runs background_main in a loop
  that restarts on exit code 75 only. The recycle waits for a quiet moment — no
  job running and no cron/hourly job due within RECYCLE_CRON_GUARD — pauses the
  scheduler, waits for in-flight jobs, then exits 75. Interval jobs simply resume
  on the new process; a daily send can't be skipped by a restart that lands on it.
"""
from __future__ import annotations

import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("rss_recycle")

RECYCLE_EXIT_CODE = 75            # EX_TEMPFAIL — start.sh restarts on exactly this
MIN_UPTIME_S = 10 * 60            # never recycle a process that just booted
CHECK_EVERY_S = 60
WEB_COOLDOWN_S = 180              # one web worker restart per container per window
RECYCLE_CRON_GUARD = timedelta(minutes=5)
DRAIN_TIMEOUT_S = 5 * 60
COOLDOWN_STAMP = "/tmp/so-web-recycle.stamp"


def rss_mb() -> float | None:
    """Resident set size of THIS process in MB (Linux /proc), None if unknown."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def _limit(env_name: str) -> int:
    try:
        return max(0, int(os.environ.get(env_name) or 0))
    except ValueError:
        return 0


class _OverLimit:
    """True once RSS has been over the limit on two consecutive checks, after the
    process has lived MIN_UPTIME_S — one spike from a big report doesn't count."""

    def __init__(self, limit_mb: int, started: float):
        self.limit_mb, self.started, self.strikes = limit_mb, started, 0

    def check(self, rss: float | None, at: float) -> bool:
        if rss is None or at - self.started < MIN_UPTIME_S:
            self.strikes = 0
            return False
        self.strikes = self.strikes + 1 if rss > self.limit_mb else 0
        return self.strikes >= 2


# ── web ──────────────────────────────────────────────────────────────────────

def claim_web_cooldown(stamp_path: str = COOLDOWN_STAMP, at: float | None = None,
                       cooldown_s: float | None = None) -> bool:
    """Atomically take the container-wide restart slot; False if a sibling worker
    restarted inside the cooldown window."""
    import fcntl

    at = time.time() if at is None else at
    cooldown_s = WEB_COOLDOWN_S if cooldown_s is None else cooldown_s
    with open(stamp_path, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read().strip()
            last = float(raw) if raw else 0.0
            if at - last < cooldown_s:
                return False
            fh.seek(0)
            fh.truncate()
            fh.write(str(at))
            fh.flush()
            return True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def start_web_worker_recycler() -> bool:
    limit = _limit("WEB_WORKER_RECYCLE_MB")
    if not limit:
        return False
    started = time.time()
    gate = _OverLimit(limit, started)

    def _loop():
        while True:
            time.sleep(CHECK_EVERY_S + random.uniform(0, 30))
            rss = rss_mb()
            if not gate.check(rss, time.time()):
                continue
            if not claim_web_cooldown():
                continue
            log.warning("recycling web worker pid=%s rss=%.0fMB > %dMB (uvicorn restarts it)",
                        os.getpid(), rss, limit)
            os.kill(os.getpid(), signal.SIGTERM)
            return

    threading.Thread(target=_loop, name="rss-recycle-web", daemon=True).start()
    log.info("web worker recycler armed: pid=%s limit=%dMB", os.getpid(), limit)
    return True


# ── scheduler worker ─────────────────────────────────────────────────────────

def _running_jobs(sched) -> int | None:
    try:
        return sum(n for ex in sched._executors.values() for n in ex._instances.values())
    except Exception:                                  # noqa: BLE001 — unknown ⇒ not idle
        return None


def _scheduled_send_due_soon(sched, now: datetime | None = None,
                             window: timedelta = RECYCLE_CRON_GUARD) -> bool:
    """A cron job (or a ≥1h interval job) due inside `window` — those are the ones
    a restart could skip. Sub-hourly interval jobs just resume on the new process."""
    from apscheduler.triggers.interval import IntervalTrigger

    horizon = (now or datetime.now(timezone.utc)) + window
    for job in sched.get_jobs():
        nrt = getattr(job, "next_run_time", None)
        if nrt is None:
            continue
        trig = job.trigger
        if isinstance(trig, IntervalTrigger) and trig.interval < timedelta(hours=1):
            continue
        if nrt <= horizon:
            return True
    return False


def scheduler_quiet(sched, now: datetime | None = None) -> bool:
    return _running_jobs(sched) == 0 and not _scheduled_send_due_soon(sched, now)


def drain_and_exit(sched, exit_fn=os._exit, sleep=time.sleep,
                   drain_timeout_s: float = DRAIN_TIMEOUT_S) -> bool:
    """Pause, let in-flight jobs finish, exit RECYCLE_EXIT_CODE. If jobs don't
    drain in time, resume and report False — try again on a later check."""
    sched.pause()
    deadline = time.monotonic() + drain_timeout_s
    while time.monotonic() < deadline:
        if _running_jobs(sched) == 0:
            try:
                sched.shutdown(wait=False)
            except Exception:                          # noqa: BLE001
                pass
            logging.shutdown()
            exit_fn(RECYCLE_EXIT_CODE)
            return True
        sleep(2)
    sched.resume()
    log.warning("worker recycle aborted: jobs still running after %ss", drain_timeout_s)
    return False


def start_scheduler_recycler(sched) -> bool:
    limit = _limit("WORKER_RECYCLE_MB")
    # Only under start.sh's restart loop — a bare exit would stop the scheduler.
    if not limit or os.environ.get("SO_SUPERVISED") != "1":
        return False
    gate = _OverLimit(limit, time.time())

    def _loop():
        while True:
            time.sleep(CHECK_EVERY_S)
            rss = rss_mb()
            if not gate.check(rss, time.time()):
                continue
            if not scheduler_quiet(sched):
                continue
            log.warning("recycling scheduler worker pid=%s rss=%.0fMB > %dMB "
                        "(start.sh restarts it)", os.getpid(), rss, limit)
            if drain_and_exit(sched):
                return

    threading.Thread(target=_loop, name="rss-recycle-worker", daemon=True).start()
    log.info("scheduler worker recycler armed: pid=%s limit=%dMB", os.getpid(), limit)
    return True
