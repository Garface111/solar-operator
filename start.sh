#!/bin/sh
# Shared entrypoint for both Railway services in this repo (they share this
# railway.toml). The harvester service sets CLOUD_CAPTURE_HARVESTER=1 → it runs
# the headless browser loop with a manually-started virtual display (xvfb-run can
# hang in a container). harvester_main.py serves /health so the shared
# healthcheck passes. EVERY other service (the web API) runs migrate + uvicorn
# EXACTLY as before.
if [ "$CLOUD_CAPTURE_HARVESTER" = "1" ]; then
  echo "start.sh: launching Cloud Capture harvester (Xvfb :99)"
  Xvfb :99 -screen 0 1366x900x24 >/tmp/xvfb.log 2>&1 &
  export DISPLAY=:99
  sleep 3
  exec python -u harvester_main.py
fi
python -m api.migrate && exec uvicorn api.app:app --host 0.0.0.0 --port "${PORT:-8000}"
