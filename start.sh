#!/bin/sh
# Shared entrypoint for Railway services in this repo (they share railway.toml).
#
# Roles (first match wins):
#   CLOUD_CAPTURE_HARVESTER=1  → headless browser harvester + /health
#   PROCESS_ROLE=worker
#     or SO_PROCESS=worker     → APScheduler + Sovereign + minimal /health
#                                (api.background_main; no public product API)
#   default                    → migrate + uvicorn api.app (public web/API)
#
# Process split env (ops):
#   web:    RUN_SCHEDULER=0
#   worker: PROCESS_ROLE=worker  RUN_SCHEDULER=1
# RUN_SCHEDULER defaults to 1 inside the app for single-process backwards compat.
#
# WARNING - DEPLOY TRAP (2026-07-19): the `worker` service is the ONLY one running
# the scheduler (RUN_SCHEDULER=1) - every job in api/scheduler.py and api/jobs/*
# (generation-report sends, morning digest, pre-send reviews, delivery receipts,
# co-op session warnings, alert sweeps) runs THERE and nowhere else. It has been
# observed NOT auto-deploying on push while `web` and `cloud-capture-harvester`
# (same repo, same branch, empty Watch Paths) deployed fine - so a scheduler
# change can merge, "deploy", and silently never run.
#
# After ANY scheduler change, VERIFY a worker deployment newer than your push:
#     railway deployment list --service worker --environment production
# If it is stale, deploy it explicitly (builds identically; role is env-driven):
#     git -C <repo> fetch origin
#     git worktree add --detach /tmp/so-worker origin/main
#     cd /tmp/so-worker && railway link \
#         --project 7451f2d4-6d29-41de-b8f4-a7461052a578 \
#         --environment production --service worker
#     railway up --service worker --environment production --detach

if [ "$CLOUD_CAPTURE_HARVESTER" = "1" ]; then
  echo "start.sh: launching Cloud Capture harvester (Xvfb :99)"
  Xvfb :99 -screen 0 1366x900x24 >/tmp/xvfb.log 2>&1 &
  export DISPLAY=:99
  sleep 3
  exec python -u harvester_main.py
fi

if [ "$PROCESS_ROLE" = "worker" ] || [ "$SO_PROCESS" = "worker" ]; then
  echo "start.sh: launching background worker (scheduler + /health)"
  # Migrations stay on the web service; worker just needs tables to exist.
  export RUN_SCHEDULER="${RUN_SCHEDULER:-1}"
  exec python -m api.background_main
fi

# Multi-worker web: a single blocked event loop was taking the whole public
# site dark (TLS accepted, /health hung with 0 bytes) while Railway still
# showed "Online". Default 2 workers so one stuck request path can't freeze
# the product. Override with WEB_CONCURRENCY.
WORKERS="${WEB_CONCURRENCY:-2}"
# Cap at 4 on small containers — more workers thrash RAM without more CPU.
case "$WORKERS" in
  ''|*[!0-9]*) WORKERS=2 ;;
esac
if [ "$WORKERS" -lt 1 ]; then WORKERS=1; fi
if [ "$WORKERS" -gt 4 ]; then WORKERS=4; fi
echo "start.sh: launching web API (uvicorn workers=$WORKERS)"
python -m api.migrate && exec uvicorn api.app:app --host 0.0.0.0 --port "${PORT:-8000}" --workers "$WORKERS"
