#!/bin/bash
# BankAI permission self-heal. Lives in /root (outside the deploy tree, so a
# redeploy cannot overwrite it) and runs as root via a systemd timer + as
# ExecStartPre on bankai.service. Deploys from a /mnt/c (DrvFs) worktree keep
# copying the source in at mode 0777 — this restores owner(root)-only, which is
# safe because both BankAI services run as root and nothing non-root needs the
# tree. Idempotent and cheap: it only touches files that have actually drifted,
# and only records to the tamper-evident ledger when it changed something.
#
# Never fails the caller (so it can gate service start): always exits 0.

BASE=/opt/bankai
DATA=/root/bankai-data

# Files under BASE with any group/other bit set = drift.
drift=$(find "$BASE" -perm /077 2>/dev/null | wc -l)

if [ "$drift" -gt 0 ]; then
  chown -R root:root "$BASE" 2>/dev/null
  find "$BASE" -perm /077 -exec chmod go-rwx {} + 2>/dev/null
  chmod 600 "$BASE/.env" 2>/dev/null

  # Best-effort audit-ledger entry so the auto-heal itself is recorded.
  ( cd "$BASE" && /opt/bankai/venv/bin/python - "$drift" <<'PY'
import sys
try:
    from bankai.db import session_scope
    from bankai.security import sentinel
    n = sys.argv[1] if len(sys.argv) > 1 else "?"
    with session_scope() as s:
        sentinel.record_event(
            s, kind="perms_relocked", severity="notice", actor="system",
            summary=f"auto-heal: re-locked {n} drifted path(s) under /opt/bankai to owner-only",
        )
except Exception:
    pass
PY
  ) 2>/dev/null
fi

# Re-assert the runtime data perms (cheap, no recursion needed).
chmod 700 "$DATA" 2>/dev/null
chmod 600 "$DATA"/bankai.db "$DATA"/bankai.db-wal "$DATA"/bankai.db-shm "$DATA"/server.log 2>/dev/null

exit 0
