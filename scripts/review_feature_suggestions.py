#!/usr/bin/env python3
"""Claude Code agent review of queued Array Operator feature suggestions.

Pulls 'new' suggestions from the backend, runs `claude -p` (codebase-aware, plan
mode = read-only) to review each, and posts the review back — which emails Ford.
Run on-demand or by cron. SAFE: review-only; never edits code or deploys.
"""
import json
import os
import subprocess
import sys
import urllib.request

BASE = os.getenv("AO_API_BASE", "https://web-production-49c83.up.railway.app")
KEY = os.getenv("ADMIN_API_KEY", "")
REPO = os.getenv("AO_REPO", "/root/solar-operator")
LIMIT = int(os.getenv("FS_REVIEW_LIMIT", "5"))

PROMPT = """You are reviewing a CUSTOMER feature suggestion for Array Operator, a solar-fleet \
monitoring + utility-bill billing SaaS (FastAPI backend in api/, static/React frontends). \
Inspect this codebase as needed to ground your answer.

Suggestion (from {email}):
\"\"\"
{text}
\"\"\"

Write a concise review for the founder:
1. What they're really asking for (1 line).
2. Feasibility against the current codebase (where it'd live; blockers, if any).
3. Rough effort: S / M / L, and why.
4. Product fit + value.
5. Recommendation: BUILD NOW / BACKLOG / PASS, with a one-line reason.
Keep it under ~200 words. Do NOT modify any files."""


def _get(path):
    with urllib.request.urlopen(urllib.request.Request(BASE + path), timeout=30) as r:
        return json.loads(r.read())


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def review_one(s):
    prompt = PROMPT.format(email=s.get("email") or "anonymous", text=s["text"])
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--permission-mode", "plan"],
            cwd=REPO, capture_output=True, text=True, timeout=600)
        return (out.stdout or "").strip() or (out.stderr or "").strip() or "(no agent output)"
    except Exception as e:
        return f"(agent review failed: {e})"


def main():
    if not KEY:
        print("review: ADMIN_API_KEY not set — skipping")
        return
    sugg = _get(f"/admin/feature-suggestions?status=new&key={KEY}").get("suggestions", [])
    if not sugg:
        print("review: no new suggestions")
        return
    for s in sugg[:LIMIT]:
        print(f"reviewing #{s['id']}: {s['text'][:60]!r} ...")
        review = review_one(s)
        _post(f"/admin/feature-suggestions/{s['id']}/review?key={KEY}", {"review": review})
        print(f"  posted review for #{s['id']} ({len(review)} chars)")


if __name__ == "__main__":
    main()
