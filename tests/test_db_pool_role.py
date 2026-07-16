"""Role-aware DB pool defaults (web headroom vs worker self-cap).

Tests pure helpers only — no engine recreate required.
"""
from __future__ import annotations

import pytest

from api.db import (
    process_is_worker,
    resolve_pool_defaults,
    pool_config_summary,
    _WEB_POOL_SIZE,
    _WEB_MAX_OVERFLOW,
    _WORKER_POOL_SIZE,
    _WORKER_MAX_OVERFLOW,
)


# ── process_is_worker ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, False),
        ({"PROCESS_ROLE": "web"}, False),
        ({"PROCESS_ROLE": "api"}, False),
        ({"PROCESS_ROLE": "worker"}, True),
        ({"PROCESS_ROLE": "background"}, True),
        ({"PROCESS_ROLE": "scheduler"}, True),
        ({"SO_PROCESS": "worker"}, True),
        ({"SO_PROCESS": "background"}, True),
        ({"RAILWAY_SERVICE_NAME": "worker"}, True),
        ({"RAILWAY_SERVICE_NAME": "solar-worker"}, True),
        ({"RAILWAY_SERVICE_NAME": "background-jobs"}, True),
        ({"RAILWAY_SERVICE": "my-background"}, True),
        ({"RAILWAY_SERVICE_NAME": "web"}, False),
        ({"RAILWAY_SERVICE_NAME": "api"}, False),
        # Explicit web wins over service name containing worker
        ({"PROCESS_ROLE": "web", "RAILWAY_SERVICE_NAME": "worker"}, False),
        # Explicit worker wins over web-like service name
        ({"PROCESS_ROLE": "worker", "RAILWAY_SERVICE_NAME": "web"}, True),
        # Case / whitespace
        ({"PROCESS_ROLE": "  Worker  "}, True),
        ({"RAILWAY_SERVICE_NAME": "Cloud-Background-1"}, True),
    ],
)
def test_process_is_worker(env, expected):
    assert process_is_worker(env) is expected


# ── resolve_pool_defaults ─────────────────────────────────────────────────────

def test_web_defaults_when_unset():
    cfg = resolve_pool_defaults({})
    assert cfg["role"] == "web"
    assert cfg["pool_size"] == _WEB_POOL_SIZE == 15
    assert cfg["max_overflow"] == _WEB_MAX_OVERFLOW == 15
    assert cfg["capacity"] == 30
    assert cfg["pool_size_explicit"] is False
    assert cfg["max_overflow_explicit"] is False


def test_worker_defaults_when_unset():
    cfg = resolve_pool_defaults({"PROCESS_ROLE": "worker"})
    assert cfg["role"] == "worker"
    assert cfg["pool_size"] == _WORKER_POOL_SIZE == 6
    assert cfg["max_overflow"] == _WORKER_MAX_OVERFLOW == 4
    assert cfg["capacity"] == 10
    assert cfg["pool_size_explicit"] is False
    assert cfg["max_overflow_explicit"] is False


@pytest.mark.parametrize("role_env", [
    {"PROCESS_ROLE": "background"},
    {"SO_PROCESS": "scheduler"},
    {"RAILWAY_SERVICE_NAME": "worker"},
])
def test_worker_like_roles_use_small_pool(role_env):
    cfg = resolve_pool_defaults(role_env)
    assert cfg["role"] == "worker"
    assert cfg["pool_size"] == 6
    assert cfg["max_overflow"] == 4
    assert cfg["capacity"] == 10


def test_explicit_pool_size_honored_on_web():
    cfg = resolve_pool_defaults({"DB_POOL_SIZE": "20"})
    assert cfg["role"] == "web"
    assert cfg["pool_size"] == 20
    assert cfg["max_overflow"] == 15  # default overflow still
    assert cfg["pool_size_explicit"] is True
    assert cfg["max_overflow_explicit"] is False
    assert cfg["capacity"] == 35


def test_explicit_overflow_honored_on_worker():
    cfg = resolve_pool_defaults({
        "PROCESS_ROLE": "worker",
        "DB_MAX_OVERFLOW": "12",
    })
    assert cfg["role"] == "worker"
    assert cfg["pool_size"] == 6  # default worker size
    assert cfg["max_overflow"] == 12
    assert cfg["pool_size_explicit"] is False
    assert cfg["max_overflow_explicit"] is True
    assert cfg["capacity"] == 18


def test_both_explicit_on_worker():
    cfg = resolve_pool_defaults({
        "PROCESS_ROLE": "worker",
        "DB_POOL_SIZE": "8",
        "DB_MAX_OVERFLOW": "2",
    })
    assert cfg["pool_size"] == 8
    assert cfg["max_overflow"] == 2
    assert cfg["capacity"] == 10
    assert cfg["pool_size_explicit"] is True
    assert cfg["max_overflow_explicit"] is True


def test_empty_string_env_treated_as_unset():
    """Empty DB_POOL_* must not force int('') crash; use role defaults."""
    cfg = resolve_pool_defaults({
        "PROCESS_ROLE": "worker",
        "DB_POOL_SIZE": "",
        "DB_MAX_OVERFLOW": "   ",
    })
    assert cfg["pool_size"] == 6
    assert cfg["max_overflow"] == 4
    assert cfg["pool_size_explicit"] is False
    assert cfg["max_overflow_explicit"] is False


def test_defaults_dict_documents_budgets():
    cfg = resolve_pool_defaults({})
    assert cfg["defaults"]["web"] == {"pool_size": 15, "max_overflow": 15}
    assert cfg["defaults"]["worker"] == {"pool_size": 6, "max_overflow": 4}


# ── pool_config_summary (uses process env + module-level resolved sizes) ─────

def test_pool_config_summary_keys():
    summary = pool_config_summary()
    for k in ("role", "pool_size", "max_overflow", "capacity", "pool_timeout_s", "is_sqlite"):
        assert k in summary
    assert summary["role"] in ("web", "worker")
    assert summary["capacity"] == summary["pool_size"] + summary["max_overflow"]
    assert isinstance(summary["is_sqlite"], bool)
