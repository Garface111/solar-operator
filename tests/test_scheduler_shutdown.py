"""Regression: APScheduler must not ERROR on post-shutdown job submit.

Sentry PYTHON-FASTAPI-N — RuntimeError: cannot schedule new futures after
shutdown — fires when concurrent.futures tears down its pool while the
scheduler daemon is still submitting (deploys / SIGTERM).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from api.scheduler import _ShutdownSafeExecutor, stop


def test_shutdown_safe_executor_swallows_closed_pool_submit():
    """After the underlying pool is shut down, submit_job must not raise."""
    ex = _ShutdownSafeExecutor(max_workers=1)
    sched = MagicMock()
    sched._create_lock = lambda: threading.RLock()
    ex.start(sched, "default")

    ex._pool.shutdown(wait=False)

    job = MagicMock()
    job.id = "run_pending_jobs"
    job.max_instances = 1
    # Must not raise RuntimeError("cannot schedule new futures after shutdown")
    ex.submit_job(job, [datetime.now(timezone.utc)])


def test_shutdown_safe_executor_reraises_other_runtime_errors(monkeypatch):
    """Non-shutdown RuntimeErrors still propagate."""
    ex = _ShutdownSafeExecutor(max_workers=1)
    sched = MagicMock()
    sched._create_lock = lambda: threading.RLock()
    ex.start(sched, "default")

    def boom(*_a, **_k):
        raise RuntimeError("something else broke")

    monkeypatch.setattr(ex._pool, "submit", boom)

    job = MagicMock()
    job.id = "x"
    job.max_instances = 1
    with pytest.raises(RuntimeError, match="something else broke"):
        ex.submit_job(job, [datetime.now(timezone.utc)])


def test_stop_is_idempotent_when_not_running():
    """stop() is a no-op when the global scheduler is not running."""
    stop()
    stop()  # second call must not raise
