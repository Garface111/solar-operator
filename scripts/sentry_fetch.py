#!/usr/bin/env python3
"""Fetch new unresolved Sentry issues for the EnergyAgent project and emit a
compact JSON digest (with the latest event's stack trace) for the auto-fix cron.

Auth: reads a Sentry API token from (first found):
  1. $SENTRY_AUTH_TOKEN
  2. /root/.hermes/secrets/sentry_auth_token  (preferred — gitignored, off-repo)

The token only needs READ scopes (event:read, project:read, org:read). Issue
resolution is handled separately (and only after a PR merges), so a read-only
token is enough to run the triage.

State: records already-seen issue IDs in
  /root/.hermes/secrets/sentry_processed.json
so the cron never re-files a PR for the same issue. Pass --all to ignore state
(manual/debug). Pass --limit N to cap.

Output (stdout): JSON {"ok":bool, "new_issues":[...], "error":str?}. Each issue:
  id, short_id, title, culprit, level, count, user_count, permalink,
  first_seen, last_seen, stack (trimmed latest-event frames as text).
Designed to be injected as cron context — prints nothing sensitive.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

SENTRY_BASE = "https://sentry.io/api/0"
TOKEN_FILE = "/root/.hermes/secrets/sentry_auth_token"
STATE_FILE = "/root/.hermes/secrets/sentry_processed.json"
TIMEOUT = 30


def _token() -> str:
    t = os.getenv("SENTRY_AUTH_TOKEN", "").strip()
    if t:
        return t
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _get(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _discover_project(token: str):
    """Return (org_slug, project_slug) for the first project the token can see.
    The EnergyAgent backend has a single Sentry project, so the first is correct.
    """
    projects = _get(f"{SENTRY_BASE}/projects/", token)
    if not projects:
        raise RuntimeError("token sees no Sentry projects")
    p = projects[0]
    return p["organization"]["slug"], p["slug"]


def _load_state() -> set:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("seen", []))
    except (FileNotFoundError, ValueError):
        return set()


def _save_state(seen: set) -> None:
    # Keep the last ~2000 ids so the file can't grow unbounded.
    trimmed = list(seen)[-2000:]
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"seen": trimmed}, f)
    os.replace(tmp, STATE_FILE)


def _latest_stack(issue_id: str, token: str) -> str:
    """Best-effort: pull the latest event and render a short stack trace."""
    try:
        ev = _get(f"{SENTRY_BASE}/issues/{issue_id}/events/latest/", token)
    except Exception:
        return ""
    lines = []
    for entry in ev.get("entries", []):
        if entry.get("type") != "exception":
            continue
        for val in entry.get("data", {}).get("values", []) or []:
            etype = val.get("type", "")
            emsg = val.get("value", "")
            lines.append(f"{etype}: {emsg}")
            frames = (val.get("stacktrace") or {}).get("frames", []) or []
            # innermost frames are most relevant; show the last ~12
            for fr in frames[-12:]:
                fn = fr.get("filename") or fr.get("module") or "?"
                func = fr.get("function") or "?"
                ln = fr.get("lineno")
                ctx = fr.get("context_line") or ""
                lines.append(f"  {fn}:{ln} in {func}()  {ctx.strip()[:120]}")
    return "\n".join(lines)[:4000]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore processed-state")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--mark", action="store_true",
                    help="record returned issue ids as seen (the cron passes this)")
    args = ap.parse_args()

    token = _token()
    if not token:
        print(json.dumps({"ok": False, "error": "no_token",
                          "hint": f"put a Sentry API token in {TOKEN_FILE} or $SENTRY_AUTH_TOKEN"}))
        return 0  # exit 0 so the cron stays quiet/no-op until the token exists

    try:
        org, proj = _discover_project(token)
        url = (f"{SENTRY_BASE}/projects/{org}/{proj}/issues/"
               f"?query=is:unresolved&statsPeriod=24h&limit={args.limit}")
        issues = _get(url, token)
    except urllib.error.HTTPError as e:
        print(json.dumps({"ok": False, "error": f"http_{e.code}",
                          "detail": e.read().decode()[:300]}))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}))
        return 0

    seen = set() if args.all else _load_state()
    out = []
    new_ids = []
    for it in issues:
        iid = it.get("id")
        if not iid or iid in seen:
            continue
        new_ids.append(iid)
        out.append({
            "id": iid,
            "short_id": it.get("shortId"),
            "title": it.get("title"),
            "culprit": it.get("culprit"),
            "level": it.get("level"),
            "count": it.get("count"),
            "user_count": it.get("userCount"),
            "permalink": it.get("permalink"),
            "first_seen": it.get("firstSeen"),
            "last_seen": it.get("lastSeen"),
            "stack": _latest_stack(iid, token),
        })

    if args.mark and new_ids:
        _save_state(seen | set(new_ids))

    print(json.dumps({"ok": True, "org": org, "project": proj,
                      "new_count": len(out), "new_issues": out}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
