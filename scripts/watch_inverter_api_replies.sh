#!/usr/bin/env bash
# Cron wrapper — watch Gmail for the Fronius / SMA inverter-API enrollment replies
# and advance the API linking (see scripts/watch_inverter_api_replies.py +
# HANDOFF_API_VERIFICATION.md). No-ops cleanly until the Gmail app password exists
# at ~/.hermes/secrets/gmail_app_password, so it's safe to schedule beforehand.
set -uo pipefail
cd /root/solar-operator || exit 1
echo "=== $(date -u +%FT%TZ) inverter-api reply watch ==="
.venv/bin/python -m scripts.watch_inverter_api_replies
