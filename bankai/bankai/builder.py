"""Approved code changes become real code — by an autonomous builder agent.

Ford's decision (2026-08-11): approving a `code_change` action on the dashboard
should not merely file the idea for a human; it should dispatch an agent that
implements it. This is that dispatcher.

What runs where, and why it is safe enough to run at all:

- The builder is a headless Claude Code agent (Opus 4.8) working in the
  DEVELOPMENT WORKTREE. It never touches `/opt/bankai`, the live database, the
  document vault, or `.env` — it edits source, and nothing it edits is running
  at the time it edits it.
- The builder is told not to commit, push, or deploy. It implements and tests;
  this module then checks the work and decides. That keeps the chain of custody
  in code that the builder did not write during this run.
- Three gates stand between an approved idea and production, and all three are
  enforced HERE rather than trusted from the agent's own report:
    1. SCOPE — `git diff` must show changes only under the copilot's package
       and tests (`selfimprove.ALLOWED_PREFIXES`, the same guard the propose
       path uses). A build that touched a systemd unit, a credential, or
       another project is refused and left uncommitted for a human.
    2. TESTS — the full suite is re-run by this module. The agent saying
       "tests pass" is not evidence; a green run in our own subprocess is.
    3. DEPLOY — only after 1 and 2. Deployment happens in a transient systemd
       scope so restarting bankai.service cannot kill the process performing
       the restart.
- Only a human dashboard click (behind APP_PASSWORD) reaches this module. The
  copilot can propose its own changes all day; it cannot approve them.

Honest bootstrap note: this file is itself inside the buildable surface, so a
build could rewrite these guards — but only for the NEXT run, since the guards
enforcing any given build are the deployed ones that started it. The protection
is real but it is one generation deep; a human reading the diff is still the
last line, which is why every build reports what changed.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from . import config, selfimprove
from .db import session_scope
from .models import AgentAction, ChatMessage

log = logging.getLogger("bankai.builder")

#: One build at a time. The worktree is shared with human sessions and other
#: agents, so two concurrent builders would interleave edits into one diff.
_LOCK = threading.Lock()

BUILDER_SPEAKER = "builder (automatic)"

#: Paths a build may touch, relative to the REPO root (the app lives in the
#: repo's bankai/ subdirectory, so the package is bankai/bankai/).
_ALLOWED_REPO_PREFIXES = tuple(
    f"bankai/{prefix}" for prefix in selfimprove.ALLOWED_PREFIXES
)


def build_prompt(action: AgentAction) -> str:
    """What the builder is told. Specific about the goal, explicit about the
    boundaries, silent about how to write the code — it is better at that than
    a prompt is."""
    return f"""You are implementing an approved change to BankAI, the household finance
copilot that Ford and Gaurav rely on. The household approved this proposal on
the dashboard just now, so it is authorized work — implement it properly.

TITLE: {action.title}

RATIONALE (written by the copilot that proposed it):
{action.rationale}

DETAIL:
{action.body}

WHERE YOU ARE
The repository is checked out at {config.BUILDER_WORKTREE}. The application
lives in the `bankai/` subdirectory: source in `bankai/bankai/`, tests in
`bankai/tests/`. Read the surrounding code first and match its style — this
codebase writes comments that explain WHY, and names things in plain language.

HOW TO TEST
    cd {config.BUILDER_WORKTREE}/bankai && {config.BUILDER_TEST_CMD}
The suite must be green when you finish. Add tests that would fail without
your change — a green suite that never exercised the new behavior proves
nothing.

BOUNDARIES (these are hard)
- Change files ONLY under `bankai/bankai/` and `bankai/tests/`. Nothing else:
  not `.env`, not systemd units, not the database, not another project.
- Do NOT commit, push, or deploy, and do not restart any service. Leave your
  work in the working tree; the dispatcher verifies it and ships it.
- Do NOT touch the live database at /root/bankai-data/ or anything in /opt.
- This app is read-only against real money by construction. No code path may
  move funds. The test `test_the_agent_has_no_tool_that_writes_source` and the
  read-only invariants must keep passing.
- If the proposal is ambiguous, implement the smallest correct reading of it
  and say plainly what you chose and what you left alone.

WHEN YOU ARE DONE
Reply with a short report: what you changed, which tests you added, the final
test result, and anything you deliberately did not do. If you could not make it
work, say so plainly — an honest failure is far more useful here than a
half-change that ships."""


def ensure_worktree() -> str:
    """Give the builder its own checkout, freshly synced to the remote.

    The first version of this built in the tree humans and other agents use,
    and refused to run whenever anyone had uncommitted work — which, on this
    machine, is most of the time. A dedicated detached worktree fixes that and
    two other things: builds can never entangle a person's in-progress edits,
    and it sits on ext4 instead of the 9p mount, so the suite runs several times
    faster.

    Detached on purpose: git will not check out the same branch in two
    worktrees, and the shared tree already holds it. Commits are pushed with an
    explicit `HEAD:<branch>` refspec.
    """
    path = Path(config.BUILDER_WORKTREE)
    branch = config.BUILDER_BRANCH
    healthy = (path / ".git").exists()
    if healthy:
        # A worktree can exist on disk and still be unusable — most often when
        # it was created by Windows git, which records its gitdir as a Windows
        # path Linux git cannot resolve. Prove it works before trusting it.
        try:
            _git(str(path), "rev-parse", "--git-dir")
        except Exception:
            log.warning("builder: build worktree at %s is broken; recreating", path)
            healthy = False
            subprocess.run(["rm", "-rf", str(path)], capture_output=True, timeout=120)
            subprocess.run(
                ["git", "worktree", "prune"], cwd=config.BUILDER_SOURCE_REPO,
                capture_output=True, timeout=120,
            )
    if not healthy:
        log.info("builder: creating build worktree at %s", path)
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=config.BUILDER_SOURCE_REPO, capture_output=True, text=True,
            timeout=300, check=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), f"origin/{branch}"],
            cwd=config.BUILDER_SOURCE_REPO, capture_output=True, text=True,
            timeout=300, check=True,
        )
    # Start every build from exactly what the remote has — never from whatever
    # the last build happened to leave behind.
    _git(str(path), "fetch", "origin", branch, timeout=300)
    _git(str(path), "checkout", "--detach", f"origin/{branch}")
    _git(str(path), "reset", "--hard", f"origin/{branch}")
    _git(str(path), "clean", "-fd")
    return str(path)


def _git(worktree: str, *args: str, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=worktree, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or '')[:300]}")
    return proc.stdout


def changed_files(worktree: str) -> list[str]:
    """Every path the build touched: modified, staged, and untracked alike.
    Untracked matters — a new file outside the allowed surface is exactly the
    thing a scope check exists to catch."""
    tracked = _git(worktree, "diff", "HEAD", "--name-only")
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard")
    seen = {p.strip() for p in (tracked + "\n" + untracked).splitlines() if p.strip()}
    return sorted(seen)


def out_of_scope(paths: list[str]) -> list[str]:
    return [p for p in paths if not p.startswith(_ALLOWED_REPO_PREFIXES)]


def run_tests(worktree: str) -> tuple[bool, str]:
    """Re-run the suite ourselves. The agent's claim is not evidence."""
    proc = subprocess.run(
        shlex.split(config.BUILDER_TEST_CMD),
        cwd=str(Path(worktree) / "bankai"),
        capture_output=True,
        text=True,
        timeout=config.BUILDER_TEST_TIMEOUT_SECONDS,
    )
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
    summary = tail[-1] if tail else "no output"
    return proc.returncode == 0, summary[:300]


#: Written at deploy time, outside the deployed tree — the script has to
#: outlive the process that wrote it.
_DEPLOY_SCRIPT = Path("/root/bankai-data/selfbuild-deploy.sh")

_DEPLOY_TEMPLATE = """#!/bin/bash
# Written by bankai.builder. Deploys a build and records its own outcome.
# It runs in a transient systemd scope so that stopping bankai.service — which
# kills the builder thread that launched this — cannot kill the deploy itself.
set -o pipefail
ACTION_ID="{action_id}"
LOG=/root/bankai-data/selfbuild-deploy.log
exec >>"$LOG" 2>&1
echo "=== $(date -Is) deploying build for $ACTION_ID ==="

record() {{  # append the deploy outcome to the action the household approved
  /opt/bankai/venv/bin/python - "$ACTION_ID" "$1" <<'PY'
import sys
from bankai.db import session_scope
from bankai.models import AgentAction, ChatMessage
action_id, note = sys.argv[1], sys.argv[2]
with session_scope() as s:
    a = s.get(AgentAction, action_id)
    if a is not None:
        a.result = (a.result or "")[:800] + " | " + note
        a.status = "executed" if note.startswith("deployed") else "failed"
    s.add(ChatMessage(channel="web", role="assistant",
                      speaker="builder (automatic)", content="[builder] " + note))
PY
}}

systemctl stop bankai || true
# An inbound email whose claim is orphaned by the stop is never retried.
/opt/bankai/venv/bin/python - <<'PY' || true
from bankai.db import session_scope
from bankai.connectors import resend_inbound
from sqlalchemy import text
with session_scope() as s:
    for row in s.execute(text("select resend_id from inbound_emails where outcome='claimed'")):
        resend_inbound.release(s, row[0])
PY

if ! rsync -a --delete --exclude '.env' --exclude 'venv' --exclude '__pycache__' \
     --exclude '.pytest_cache' --exclude 'bankai.db*' --exclude 'documents' \
     --exclude 'reports' {worktree}/bankai/ /opt/bankai/; then
  systemctl start bankai
  record "DEPLOY FAILED during file sync — the previous version is running"
  exit 1
fi
/opt/bankai/venv/bin/pip install -q -r /opt/bankai/requirements.txt || true

systemctl start bankai
sleep 5
if /opt/bankai/venv/bin/python -c "import urllib.request,sys; \
sys.exit(0 if b'true' in urllib.request.urlopen('http://127.0.0.1:8300/api/health', timeout=10).read() else 1)"; then
  record "deployed and healthy — the change is live"
else
  record "DEPLOY FAILED — service did not come back healthy; check journalctl -u bankai"
fi
"""


def deploy(action_id: str) -> str:
    """Ship it, detached, and let the deploy report its own result.

    This call happens inside bankai.service, and the deploy stops that service —
    which kills this thread mid-sentence. So nothing after this function can be
    relied on to run: the script itself records whether the deploy worked, and
    posts it to the thread. Fire, and do not expect to come back."""
    _DEPLOY_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    _DEPLOY_SCRIPT.write_text(
        _DEPLOY_TEMPLATE.format(
            action_id=action_id, worktree=shlex.quote(config.BUILDER_WORKTREE)
        )
    )
    _DEPLOY_SCRIPT.chmod(0o700)
    proc = subprocess.run(
        [
            "systemd-run", "--collect",
            f"--unit=bankai-selfbuild-{datetime.utcnow():%Y%m%d%H%M%S}",
            "/bin/bash", str(_DEPLOY_SCRIPT),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not launch the deploy (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[:300]}"
        )
    return "deploy launched"


def _tell_household(text: str) -> None:
    """A plain thread message — no model call. The household should hear that
    their software changed from the software itself, not from a log file."""
    try:
        with session_scope() as session:
            session.add(ChatMessage(
                channel="web", role="assistant",
                speaker=BUILDER_SPEAKER, content=text,
            ))
    except Exception:
        log.exception("builder: could not post to the thread")


def _finish(action_id: str, status: str, note: str) -> dict:
    with session_scope() as session:
        action = session.get(AgentAction, action_id)
        if action is not None:
            action.status = status
            action.result = note[:1000]
            action.executed_at = datetime.utcnow()
    log.info("builder %s: %s — %s", action_id, status, note[:200])
    return {"status": status, "note": note}


def run_build(action_id: str) -> dict:
    """The whole pipeline, start to finish. Safe to call in a worker thread."""
    if not _LOCK.acquire(blocking=False):
        return _finish(action_id, "proposed",
                       "another build is already running — approve this again once it finishes")
    try:
        with session_scope() as session:
            action = session.get(AgentAction, action_id)
            if action is None:
                return {"status": "error", "note": "action not found"}
            title, prompt = action.title, build_prompt(action)

        try:
            worktree = ensure_worktree()
        except Exception as exc:
            return _finish(action_id, "failed", f"build worktree unusable: {exc}")

        cmd = [
            config.CLAUDE_CLI_BIN, "-p", prompt,
            "--model", config.BUILDER_MODEL,
            "--effort", config.BUILDER_EFFORT,
            "--add-dir", worktree,
            "--permission-mode", "acceptEdits",
            "--max-turns", str(config.BUILDER_MAX_TURNS),
            "--output-format", "json",
        ]
        log.info("builder: starting %s on %s", config.BUILDER_MODEL, title)
        try:
            proc = subprocess.run(
                cmd, cwd=worktree, capture_output=True, text=True,
                timeout=config.BUILDER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return _finish(action_id, "failed", (
                f"the builder ran past {config.BUILDER_TIMEOUT_SECONDS}s and was stopped; "
                "any partial edits were left in the worktree for a human"
            ))
        if proc.returncode != 0:
            return _finish(action_id, "failed",
                           f"builder exited {proc.returncode}: {(proc.stderr or '')[:200]}")

        report = ""
        try:
            import json as _json
            report = (_json.loads(proc.stdout).get("result") or "").strip()
        except Exception:
            report = (proc.stdout or "")[-600:]

        # GATE 1 — scope. Checked against git, not against what the agent says.
        touched = changed_files(worktree)
        if not touched:
            return _finish(action_id, "failed",
                           f"the builder changed nothing. It reported: {report[:400]}")
        stray = out_of_scope(touched)
        if stray:
            return _finish(action_id, "failed", (
                f"REFUSED — the build touched files outside the copilot's own source: "
                f"{', '.join(stray[:5])}. Nothing was committed or deployed; the "
                "changes are in the worktree for a human to review."
            ))

        # GATE 2 — tests, run by us.
        green, summary = run_tests(worktree)
        if not green:
            return _finish(action_id, "failed", (
                f"the change was written but the suite is red ({summary}). Nothing "
                "deployed; the work is in the worktree. Builder's report: "
                f"{report[:300]}"
            ))

        # Commit exactly what passed the gates — never `git add -A` in a tree
        # shared with other sessions.
        _git(worktree, "add", *touched)
        message = (
            f"{title}\n\n"
            f"Implemented by an automatic builder ({config.BUILDER_MODEL}) after the "
            f"household approved action {action_id} on the dashboard.\n\n"
            f"Builder's report:\n{report[:1500]}\n\n"
            f"Gates passed: scope ({len(touched)} files, all under bankai/), "
            f"tests ({summary}).\n\n"
            "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
        )
        _git(worktree, "commit", "-m", message)
        sha = _git(worktree, "rev-parse", "--short", "HEAD").strip()
        pushed = True
        try:
            # Explicit refspec: the build worktree is detached, so there is no
            # current branch name to push by.
            _git(worktree, "push", "origin", f"HEAD:{config.BUILDER_BRANCH}", timeout=180)
        except Exception as exc:
            pushed = False
            log.warning("builder: push failed (%s) — commit %s is local only", exc, sha)

        # Record the outcome BEFORE deploying. The deploy stops the service this
        # thread runs in, so nothing below the deploy call is guaranteed to
        # execute — writing the result afterwards would leave every successful
        # build looking unfinished forever.
        note = (
            f"implemented by {config.BUILDER_MODEL}, committed {sha}"
            f"{'' if pushed else ' (local only — push failed)'}, tests {summary}"
        )
        _tell_household(
            f"[builder] {title}\n\n{report[:800]}\n\n"
            f"({len(touched)} files changed, {summary}, commit {sha}.)"
        )
        _finish(action_id, "executed", note)

        # GATE 3 — deploy. Detached and self-reporting: it appends its own
        # result to this action and posts to the thread, because by the time it
        # finishes, this process is gone.
        if not config.BUILDER_AUTO_DEPLOY:
            return {"status": "executed", "note": note + ", not deployed (auto-deploy off)"}
        try:
            deploy(action_id)
        except Exception as exc:
            return _finish(action_id, "failed", (
                f"{note}, but the deploy could not be launched: {exc}. "
                "The running install is untouched."
            ))
        return {"status": "executed", "note": note + ", deploy launched"}
    except Exception as exc:  # never leave an approved action stuck in limbo
        log.exception("builder: unexpected failure")
        return _finish(action_id, "failed", f"builder crashed: {str(exc)[:300]}")
    finally:
        _LOCK.release()


def spawn(action_id: str) -> str:
    """Fire the build and return immediately — the dashboard click must not
    wait out a coding session.

    Deliberately NOT a thread. The first version ran the build in-process, and
    every one of them died with SIGTERM: this service is restarted constantly
    (deploys, the cockpit watchdog), and a restart SIGKILLs the entire cgroup,
    agent subprocess included. A build that takes ten minutes cannot live inside
    a service that gets restarted every two. The transient unit outlives us —
    which also means a build survives the very deploy it triggers."""
    unit = f"bankai-build-{action_id[-12:]}-{datetime.utcnow():%H%M%S}"
    proc = subprocess.run(
        [
            "systemd-run", "--collect", f"--unit={unit}",
            "--working-directory=/opt/bankai",
            "--setenv=PYTHONPATH=/opt/bankai",
            "/opt/bankai/venv/bin/python", "-m", "bankai.builder", action_id,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout)[:300]
        log.error("builder: could not launch %s: %s", unit, detail)
        _finish(action_id, "failed", f"could not launch the builder: {detail}")
        return ""
    log.info("builder: launched %s for %s", unit, action_id)
    return unit


def main() -> None:
    """Entry point for the transient unit: `python -m bankai.builder <action_id>`."""
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler("/root/bankai-data/builder.log"),
                  logging.StreamHandler()],
    )
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m bankai.builder <action_id>")
    result = run_build(sys.argv[1])
    log.info("builder finished: %s", result)


if __name__ == "__main__":
    main()
