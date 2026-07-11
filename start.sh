#!/bin/sh
# Shared entrypoint for both Railway services in this repo (they share this
# railway.toml). The harvester service sets CLOUD_CAPTURE_HARVESTER=1 → it runs
# the headless browser loop under Xvfb (harvester_main.py serves /health so the
# shared healthcheck passes). EVERY other service (the web API) runs migrate +
# uvicorn EXACTLY as before — this wrapper must not change web behavior.
if [ "$CLOUD_CAPTURE_HARVESTER" = "1" ]; then
  exec xvfb-run -a --server-args="-screen 0 1366x900x24" python harvester_main.py
fi
python -m api.migrate && exec uvicorn api.app:app --host 0.0.0.0 --port "${PORT:-8000}"
