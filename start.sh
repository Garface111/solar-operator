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

python -m api.migrate && exec uvicorn api.app:app --host 0.0.0.0 --port "${PORT:-8000}"
