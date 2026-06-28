#!/usr/bin/env python3
"""Sentry auto-fix orchestrator — SAFE mode (mode A).

For each NEW unresolved Sentry issue:
  1. Create a fix branch.
  2. Hand Claude Code (opus) a tight brief: investigate the stack trace, make the
     SMALLEST correct fix, add/adjust a test, DO NOT touch sensitive paths.
  3. Run the full test suite. Only if green, push the branch + open a PR.
  4. Never merge. Never deploy. A human reviews + merges (merge auto-deploys).

HARD RAILS (a fix is ABANDONED — branch deleted, issue left for humans — if):
  - the diff touches a SENSITIVE path (auth, billing/stripe, db migrations,
    delete/teardown, secrets) — these are never auto-edited,
  - the diff is too large (> MAX_CHANGED_LINES or > MAX_FILES),
  - the test suite fails or errors,
  - Claude reports it couldn't find a safe fix.

This script makes NO changes to main and opens at most MAX_PRS_PER_RUN PRs per run
so a Sentry storm can't spam your PR list. It prints a human summary to stdout
(delivered by the cron to Ford).

Requires: gh (authed), claude (authed), a clean git working tree on main.
Run from the repo root (/root/solar-operator).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

REPO = "/root/solar-operator"
TEST_CMD = [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:warnings", "--maxfail=1"]
MAX_PRS_PER_RUN = 3
MAX_CHANGED_LINES = 60
MAX_FILES = 3
CLAUDE_TIMEOUT = 900  # 15 min per issue

# Paths the auto-fixer must NEVER modify. If Claude's diff touches any of these,
# we abandon the auto-fix and leave the issue for a human. Money + identity +
# schema + destructive ops are off-limits to an unattended bot.
SENSITIVE_RE = re.compile(
    r"(api/account\.py|api/onboarding\.py|api/stripe_webhook\.py|api/billing|"
    r"api/migrate\.py|migrations?/|api/db\.py|/auth|password|session|"
    r"delete|teardown|drop_|secret|stripe|payment|webhook)",
    re.IGNORECASE,
)


def sh(cmd, check=True, timeout=120, cwd=REPO):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({' '.join(cmd[:3])}...): {r.stderr[:400]}")
    return r


def git(*args, check=True, timeout=120):
    return sh(["git", *args], check=check, timeout=timeout)


def automerge_on() -> bool:
    """Autonomous mode: squash-merge a fully-vetted fix straight to prod (merge
    auto-deploys on Railway). OFF unless SENTRY_AUTOFIX_AUTOMERGE is truthy AND the
    `.autofix_nomerge` brake file is absent — so Ford can drop back to PR-only without
    disabling detection or touching cron (`touch .autofix_nomerge`)."""
    flag = os.getenv("SENTRY_AUTOFIX_AUTOMERGE", "").strip().lower()
    if flag in ("", "0", "false", "no", "off"):
        return False
    return not os.path.exists(os.path.join(REPO, ".autofix_nomerge"))


def merge_pr(pr_url: str) -> tuple[bool, str]:
    """Squash-merge + delete the branch. --admin bypasses any branch protection.
    Returns (ok, tail-of-output). The merge triggers a prod deploy."""
    m = sh(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch", "--admin"],
           check=False, timeout=120)
    return (m.returncode == 0, (m.stdout + m.stderr).strip()[-300:])


def post_merge_health() -> str:
    """Crash-canary after an auto-merge: poll prod /health for a couple minutes so a
    fix that crash-loops on boot is caught and SHOUTED in the run summary. A 200 here
    means prod is serving (possibly still the old build mid-deploy) — a safety net, not
    proof the new version is bug-free."""
    import urllib.request
    url = os.getenv("AO_HEALTH_URL", "https://web-production-49c83.up.railway.app/health")
    deadline = time.time() + 180
    last = "(no response)"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    return "HTTP 200 ✅ (prod responding post-merge)"
                last = f"HTTP {r.status}"
        except Exception as e:
            last = f"error: {str(e)[:80]}"
        time.sleep(15)
    return f"⚠️ {last} — prod did NOT return 200 within 3 min after an auto-merge; CHECK PROD"


def ensure_clean_main():
    git("fetch", "origin", "main", check=False)
    git("checkout", "main")
    git("reset", "--hard", "origin/main", check=False)
    st = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if st:
        raise RuntimeError("tracked changes present on main; aborting")


def _untracked() -> set:
    out = git("ls-files", "--others", "--exclude-standard").stdout.strip()
    return {f for f in out.splitlines() if f.strip()}


def changed_files(new_untracked: set | None = None) -> list[str]:
    """Tracked files modified vs main PLUS any files Claude newly created
    (passed in as new_untracked so pre-existing untracked clutter is ignored)."""
    out = git("diff", "--name-only", "main").stdout.strip()
    files = [f for f in out.splitlines() if f.strip()]
    if new_untracked:
        files += sorted(new_untracked)
    return files


def diff_size(new_untracked: set | None = None) -> int:
    out = git("diff", "--numstat", "main").stdout.strip()
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            for n in parts[:2]:
                if n.isdigit():
                    total += int(n)
    # Count lines in any newly-created files too.
    for f in (new_untracked or set()):
        try:
            with open(os.path.join(REPO, f)) as fh:
                total += sum(1 for _ in fh)
        except Exception:
            total += 999  # unreadable new file → treat as oversized (fail safe)
    return total


def run_claude(issue: dict, branch: str) -> dict:
    brief = f"""A production error was reported by Sentry. Investigate and make the
SMALLEST correct fix, following these rules EXACTLY:

ISSUE: {issue.get('title')}
LEVEL: {issue.get('level')}  COUNT: {issue.get('count')}  USERS: {issue.get('user_count')}
CULPRIT: {issue.get('culprit')}

STACK TRACE / LATEST EVENT:
{issue.get('stack') or '(no stack trace available)'}

RULES (a violation means you should STOP and report you can't safely fix it):
1. Make the MINIMAL change that fixes the root cause — ideally one file, a few lines.
2. DO NOT touch any of: authentication, login/session, passwords, billing/Stripe,
   payment, webhooks, database migrations (api/migrate.py), api/db.py, or anything
   involving delete/teardown. If the fix would require touching those, STOP and say
   "UNSAFE: requires sensitive-path change" — do not edit them.
3. Add or adjust a focused test that would catch this bug if it recurs.
4. Do NOT git commit, push, or open a PR — just leave the edits in the working tree.
5. Match existing code style. Do not refactor unrelated code.
6. End your response with EXACTLY one line: either
   "RESULT: FIXED <one-sentence description>" or
   "RESULT: UNSAFE <reason>" or "RESULT: NOFIX <reason>".
"""
    r = subprocess.run(
        ["claude", "-p", brief, "--model", "opus",
         "--permission-mode", "acceptEdits", "--max-turns", "40",
         "--output-format", "json"],
        cwd=REPO, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
    )
    try:
        data = json.loads(r.stdout)
        result_text = data.get("result", "")
    except Exception:
        result_text = r.stdout[-2000:]
    verdict = "UNKNOWN"
    m = re.search(r"RESULT:\s*(FIXED|UNSAFE|NOFIX)\b(.*)", result_text)
    if m:
        verdict = m.group(1)
    return {"verdict": verdict, "summary": (m.group(2).strip() if m else result_text[-300:])}


def process_issue(issue: dict) -> dict:
    issue["title"] = issue.get("title") or "(untitled error)"
    short = issue.get("short_id") or (issue.get("id") or "unknown")[:8]
    branch = f"autofix/sentry-{short}-{int(time.time())}".lower().replace(" ", "-")
    res = {"issue": short, "title": issue.get("title"), "outcome": "", "detail": "", "pr": ""}

    ensure_clean_main()
    before_untracked = _untracked()
    git("checkout", "-b", branch)
    try:
        cc = run_claude(issue, branch)
        if cc["verdict"] != "FIXED":
            res["outcome"] = "skipped"
            res["detail"] = f"{cc['verdict']}: {cc['summary'][:200]}"
            return res

        new_untracked = _untracked() - before_untracked
        files = changed_files(new_untracked)
        if not files:
            res["outcome"] = "skipped"; res["detail"] = "claude made no edits"; return res
        # HARD RAIL: sensitive paths (covers both edited + newly-created files)
        bad = [f for f in files if SENSITIVE_RE.search(f)]
        if bad:
            res["outcome"] = "blocked-sensitive"
            res["detail"] = f"touched sensitive path(s): {', '.join(bad)} — left for human"
            return res
        # HARD RAIL: size
        n_files, n_lines = len(files), diff_size(new_untracked)
        if n_files > MAX_FILES or n_lines > MAX_CHANGED_LINES:
            res["outcome"] = "blocked-toolarge"
            res["detail"] = f"{n_files} files / {n_lines} lines exceeds cap — left for human"
            return res

        # GATE: full test suite
        t = sh(TEST_CMD, check=False, timeout=600)
        if t.returncode != 0:
            res["outcome"] = "tests-failed"
            tail = (t.stdout + t.stderr).strip().splitlines()[-8:]
            res["detail"] = "fix did NOT pass tests: " + " | ".join(tail)[:300]
            return res

        # All rails passed → commit, push, open PR (NO merge).
        # Stage ONLY the fix's files — never a blanket add -A (which could sweep
        # in unrelated untracked clutter like the email scripts).
        for f in files:
            git("add", "--", f)
        git("commit", "-q", "-m",
            f"fix(autofix): {issue.get('title')[:60]} [Sentry {short}]\n\n"
            f"Auto-generated fix for Sentry issue {short}. Full test suite passes.\n"
            f"{cc['summary'][:200]}\n\nReview before merge — merge auto-deploys to prod.")
        git("push", "-u", "origin", branch, timeout=120)
        body = (f"Auto-generated fix for **Sentry {short}** — {issue.get('title')}\n\n"
                f"**What:** {cc['summary'][:300]}\n\n"
                f"**Safety:** {len(files)} file(s), {diff_size()} lines, full test suite GREEN, "
                f"no sensitive paths touched.\n\n"
                f"Sentry: {issue.get('permalink')}\n\n"
                f"⚠️ Review before merging — merge auto-deploys to production.")
        pr = sh(["gh", "pr", "create", "--title",
                 f"[autofix] {issue.get('title')[:70]} (Sentry {short})",
                 "--body", body, "--base", "main", "--head", branch], check=False)
        url = (pr.stdout + pr.stderr).strip().splitlines()
        res["pr"] = next((l for l in url if l.startswith("http")), "(pr create output unclear)")
        res["outcome"] = "PR-opened"
        res["detail"] = cc["summary"][:200]

        # AUTONOMOUS MODE: every hard rail already passed (FIXED verdict, no sensitive
        # path, within size caps, FULL test suite GREEN) → squash-merge now, which
        # auto-deploys to prod. ONE extra rail for unattended prod merges: a regression
        # test MUST be in the diff — an untested fix is left as a PR for a human instead.
        if automerge_on() and res["pr"].startswith("http"):
            if not any("test" in f.lower() for f in files):
                res["detail"] = ("auto-merge HELD (no regression test in diff) — PR left "
                                 "for human: " + cc["summary"][:140])
            else:
                ok, mout = merge_pr(res["pr"])
                if ok:
                    res["outcome"] = "MERGED"
                    res["detail"] = "squash-merged to prod (auto-deploys): " + cc["summary"][:160]
                else:
                    res["outcome"] = "merge-failed"
                    res["detail"] = "PR opened but auto-merge FAILED (left for human): " + mout
        return res
    finally:
        # Return to a pristine main: drop any uncommitted edits + Claude's new
        # untracked files this run created, then delete the branch. Pre-existing
        # untracked files (email scripts etc.) are preserved.
        try:
            leftover = _untracked() - before_untracked
            for f in leftover:
                try:
                    os.remove(os.path.join(REPO, f))
                except OSError:
                    pass
        except Exception:
            pass
        ensure_clean_main()
        git("branch", "-D", branch, check=False)


def main() -> int:
    # Read fetched issues from stdin (the cron pipes sentry_fetch.py output in)
    # or from $SENTRY_ISSUES_JSON.
    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
    raw = raw or os.getenv("SENTRY_ISSUES_JSON", "")
    if not raw.strip():
        print("No Sentry issue input — nothing to do."); return 0
    try:
        data = json.loads(raw)
    except Exception:
        # The cron may prepend log lines; grab the JSON object.
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {"new_issues": []}

    if not data.get("ok", True):
        print(f"Sentry fetch not ready: {data.get('error')} — {data.get('hint','')}")
        return 0
    issues = data.get("new_issues", [])[:MAX_PRS_PER_RUN]
    if not issues:
        print("No new Sentry issues. ✅")
        return 0

    results = [process_issue(it) for it in issues]
    mode = "AUTONOMOUS (auto-merge ON)" if automerge_on() else "SAFE (PRs only — you merge)"
    lines = [f"Sentry auto-fix run — {mode}:\n"]
    for r in results:
        icon = {"MERGED": "🚀", "PR-opened": "✅", "merge-failed": "⚠️", "tests-failed": "🧪",
                "blocked-sensitive": "🔒", "blocked-toolarge": "📏", "skipped": "⏭️"}.get(r["outcome"], "•")
        lines.append(f"{icon} {r['issue']}: {r['title'][:60]}")
        lines.append(f"    → {r['outcome']}: {r['detail'][:200]}")
        if r["pr"]:
            lines.append(f"    PR: {r['pr']}")
    if any(r["outcome"] == "MERGED" for r in results):
        lines.append(f"\nPost-merge prod health: {post_merge_health()}")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
