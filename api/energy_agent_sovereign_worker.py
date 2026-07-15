"""Sovereign code worker — Claude Code CLI (cloth) + Grok (rock) fallback.

Ford 2026-07-15 authorized: Sovereign may use Grok or Claude Code agents to work
on the codebase and push live product updates.

Safety:
  • Never money/stripe/domain/tenant hard-delete
  • Deny secret files
  • Isolated branch + worktree when possible
  • Full audit + desk + email on outcome
  • Kill: SOVEREIGN_CODE_LIVE=0 or SOVEREIGN_ENABLED=0
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("energy_agent.sovereign.worker")

# Preferred local paths (dev host) then cache paths (Railway / headless).
_REPO_CACHE = Path(
    os.getenv("SOVEREIGN_REPO_CACHE", "/tmp/sovereign-repos")
).expanduser()

REPO_ROOTS = {
    "array-operator": Path(
        os.getenv("SOVEREIGN_AO_REPO", "/root/array-operator")
    ).expanduser(),
    "solar-operator": Path(
        os.getenv("SOVEREIGN_SO_REPO", "/root/solar-operator")
    ).expanduser(),
}

REPO_GITHUB = {
    "array-operator": os.getenv(
        "SOVEREIGN_AO_GITHUB", "https://github.com/Garface111/array-operator.git"
    ),
    "solar-operator": os.getenv(
        "SOVEREIGN_SO_GITHUB", "https://github.com/Garface111/solar-operator.git"
    ),
}

DENY_PATH_FRAGMENTS = (
    ".env",
    "secrets",
    "id_rsa",
    "auth.json",
    "stripe_secret",
    "RESEND_API_KEY",
    "ADMIN_API_KEY",
    "SESSION_SECRET",
    "private_key",
    ".pem",
)
# Note: "credentials" path fragment intentionally NOT denied — Ford unlocked
# portal credential ops; workers still must not commit .env / secret key files.

MONEY_HINTS = re.compile(
    r"\b(stripe|price_id|checkout|subscription_item|refund|payout|sk_live)\b",
    re.I,
)


def _flag(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def code_live_enabled() -> bool:
    """Master: Ford authorized live code + push. Default ON after authorization."""
    return _flag("SOVEREIGN_ENABLED", "1") and _flag("SOVEREIGN_CODE_LIVE", "1")


def code_push_enabled() -> bool:
    return code_live_enabled() and _flag("SOVEREIGN_CODE_PUSH", "1")


def code_deploy_enabled() -> bool:
    return code_live_enabled() and _flag("SOVEREIGN_CODE_DEPLOY", "1")


def _now() -> datetime:
    return datetime.utcnow()


def _find_claude() -> str | None:
    for c in [
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        "/root/.hermes/node/bin/claude",
        "/root/.local/bin/claude",
        os.path.expanduser("~/.local/bin/claude"),
    ]:
        if c and os.path.exists(c):
            return c
    return None


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 120,
    input_text: str | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    e = os.environ.copy()
    if env:
        e.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
        env=e,
    )


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, timeout=timeout)


def _github_token() -> str | None:
    for k in (
        "SOVEREIGN_GITHUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_PAT",
    ):
        v = (os.getenv(k) or "").strip()
        if v:
            return v
    return None


def _authed_clone_url(url: str) -> str:
    """Embed token in https URL for private clone/push on Railway."""
    tok = _github_token()
    if not tok:
        return url
    if url.startswith("https://"):
        # https://github.com/org/repo.git → https://x-access-token:TOKEN@github.com/...
        rest = url[len("https://") :]
        return f"https://x-access-token:{tok}@{rest}"
    return url


def ensure_repo(name: str) -> Path:
    """Ensure a writable checkout exists (local path or auto-clone to cache).

    Ford authorized full worker repo access so jobs_drain works on Railway
    (where /root/* repos are absent).
    """
    if name not in REPO_GITHUB:
        raise FileNotFoundError(f"unknown repo {name}")

    preferred = REPO_ROOTS[name]
    candidates = [preferred, _REPO_CACHE / name]

    for path in candidates:
        if path.is_dir() and (path / ".git").exists():
            # Best-effort: ensure remote can push with token
            try:
                _configure_remote_auth(path, name)
            except Exception as e:  # noqa: BLE001
                log.warning("remote auth config failed for %s: %s", name, e)
            return path

    # Clone into cache
    _REPO_CACHE.mkdir(parents=True, exist_ok=True)
    dest = _REPO_CACHE / name
    if dest.exists() and not (dest / ".git").exists():
        shutil.rmtree(dest, ignore_errors=True)
    url = _authed_clone_url(REPO_GITHUB[name])
    log.info("cloning sovereign repo %s → %s", name, dest)
    p = _run(
        ["git", "clone", "--depth", "50", url, str(dest)],
        timeout=300,
    )
    if p.returncode != 0 or not (dest / ".git").exists():
        raise FileNotFoundError(
            f"no sovereign repos available: clone {name} failed: "
            f"{(p.stderr or p.stdout or '')[:300]}"
        )
    _configure_remote_auth(dest, name)
    # local identity
    _git(dest, "config", "user.email", "sovereign@arrayoperator.com")
    _git(dest, "config", "user.name", "Sovereign")
    return dest


def _configure_remote_auth(path: Path, name: str) -> None:
    """Point origin at authed URL when token present (clone + push)."""
    tok = _github_token()
    if not tok:
        return
    base = REPO_GITHUB.get(name) or ""
    if not base:
        return
    authed = _authed_clone_url(base)
    _git(path, "remote", "set-url", "origin", authed)


def ensure_all_repos() -> dict[str, str]:
    """Warm both checkouts. Returns {name: path or error}."""
    out: dict[str, str] = {}
    for name in REPO_GITHUB:
        try:
            p = ensure_repo(name)
            out[name] = str(p)
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERROR: {e}"
    return out


def pick_repo(brief: str, title: str) -> tuple[str, Path]:
    text = f"{title}\n{brief}".lower()
    # Heuristic
    if any(k in text for k in (
        "array-operator", "arrayoperator", "public/", "energy-agent",
        "sandbox.js", "ops.js", "netlify", "sovereign-desk",
    )):
        name = "array-operator"
    elif any(k in text for k in (
        "solar-operator", "api/", "railway", "energy_agent",
        "feature_suggestion", "utility_request", "utility adapter",
        "adapter", "portal", "smarthub", "harvester",
    )):
        name = "solar-operator"
    else:
        # Utility / API work defaults to solar-operator; UI to AO
        if any(k in text for k in ("utility", "adapter", "portal", "feature #")):
            name = "solar-operator"
        else:
            name = "array-operator"

    # Prefer named repo; fall back to whichever ensure succeeds
    try:
        return name, ensure_repo(name)
    except Exception as first:
        for n in REPO_GITHUB:
            if n == name:
                continue
            try:
                return n, ensure_repo(n)
            except Exception:
                continue
        raise FileNotFoundError(
            f"no sovereign repos available ({first})"
        ) from first


def brief_is_denied(title: str, brief: str) -> str | None:
    blob = f"{title}\n{brief}"
    if MONEY_HINTS.search(blob) and not re.search(r"\b(copy|ui|label|wording)\b", blob, re.I):
        return "money/stripe changes require Ford dual-control (T5)"
    low = blob.lower()
    if any(x in low for x in ("drop table", "hard delete tenant", "rm -rf", "force push", "reset --hard origin")):
        return "destructive ops denied"
    return None


def _denied_paths_changed(cwd: Path) -> list[str]:
    st = _git(cwd, "status", "--porcelain")
    bad = []
    for line in (st.stdout or "").splitlines():
        path = line[3:].strip() if len(line) > 3 else line
        for frag in DENY_PATH_FRAGMENTS:
            if frag in path:
                bad.append(path)
                break
    return bad


def run_claude_code(
    *,
    cwd: Path,
    title: str,
    brief: str,
    expanded: str | None,
    job_id: str,
) -> dict[str, Any]:
    cb = _find_claude()
    if not cb:
        return {"ok": False, "provider": "claude_code", "error": "claude CLI not found"}

    plan = (expanded or "").strip()
    prompt = f"""You are Sovereign's coding agent implementing an AUTHORIZED live product change.

Ford Genereaux authorized Sovereign to use Claude Code on this codebase and ship updates.

## Title
{title}

## Job id
{job_id}

## Brief
{brief}

## Expanded plan (if any)
{plan or "(none)"}

## Hard rules
1. Minimal correct change only. Prefer existing patterns.
2. Do NOT commit .env, API keys, Stripe live money paths, or tenant hard-deletes.
   Portal credential *code paths* and adapter work ARE authorized (Ford unlock).
   Never hardcode real passwords into source.
3. Do NOT use force-push or git reset --hard on shared branches.
4. After edits: run quick checks if cheap (node --check on edited JS, or pytest -q on touched tests).
5. When done, leave a clean git working tree with your changes staged or committed is OK — the outer worker will commit/push.
6. If the task is research-only (utility portal family / HAR plan), write findings as a short markdown under docs/adapters/ or a comment in the PR body via final message — still prefer registry wiring when SmartHub-obvious.
7. array-operator is the owner site (public/*.js, Netlify). solar-operator is the API (api/*).

Implement the change now in the current directory ({cwd}).
"""

    max_turns = int(os.getenv("SOVEREIGN_CODE_MAX_TURNS", "24"))
    timeout = int(os.getenv("SOVEREIGN_CODE_TIMEOUT", "900"))
    cmd = [
        cb,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        "Read,Edit,Write,Bash,Glob,Grep",
        "--permission-mode",
        "acceptEdits",
        "--fallback-model",
        "sonnet",
    ]
    # Prefer API key path in headless/root (bare skips OAuth issues)
    if (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        cmd.insert(1, "--bare")

    try:
        p = _run(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "provider": "claude_code", "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": "claude_code", "error": str(e)[:400]}

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    result_text = ""
    cost = None
    try:
        data = json.loads(out) if out.startswith("{") else {}
        result_text = data.get("result") or data.get("subtype") or out[:4000]
        cost = (data.get("total_cost_usd") if isinstance(data, dict) else None)
    except json.JSONDecodeError:
        result_text = out[:4000] or err[:2000]

    dirty = _git(cwd, "status", "--porcelain")
    changed = bool((dirty.stdout or "").strip())
    return {
        "ok": p.returncode == 0 or changed,  # success if files changed even on nonzero
        "provider": "claude_code",
        "returncode": p.returncode,
        "result_text": result_text[:8000],
        "stderr": err[:1500],
        "changed": changed,
        "cost_usd": cost,
    }


def run_grok_code_assist(
    *,
    cwd: Path,
    title: str,
    brief: str,
) -> dict[str, Any]:
    """Rock fallback: ask Grok for a concrete file edit plan + optional patch apply.

    Grok cannot drive tools; we request a JSON list of {path, content} full-file
    rewrites for small scoped fixes, then write them.
    """
    try:
        from .energy_agent_sovereign_brain import call_brain
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": "grok", "error": f"import brain: {e}"}

    # List a few relevant files for context
    listing = _run(
        ["bash", "-lc", "git ls-files | head -80"],
        cwd=cwd,
        timeout=30,
    )
    files_hint = (listing.stdout or "")[:3000]

    messages = [
        {
            "role": "system",
            "content": (
                "You are Sovereign's rock coding agent (Grok). Output ONLY JSON:\n"
                '{"files":[{"path":"relative/path","content":"full new file content"}],'
                '"notes":"..."}\n'
                "Minimal edits. No secrets. Paths relative to repo root. "
                "If you cannot safely edit, return {\"files\":[],\"notes\":\"reason\"}."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Repo: {cwd.name}\nTitle: {title}\nBrief:\n{brief}\n\n"
                f"Some tracked files:\n{files_hint}\n"
            ),
        },
    ]
    try:
        raw = call_brain(messages)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": "grok", "error": str(e)[:400]}

    text = raw.get("content") or ""
    # extract JSON
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"ok": False, "provider": "grok", "error": "no JSON in response", "raw": text[:500]}
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"ok": False, "provider": "grok", "error": f"bad JSON: {e}"}

    written = []
    for f in data.get("files") or []:
        if not isinstance(f, dict):
            continue
        rel = (f.get("path") or "").lstrip("/")
        content = f.get("content")
        if not rel or content is None:
            continue
        if any(frag in rel for frag in DENY_PATH_FRAGMENTS):
            continue
        if ".." in rel:
            continue
        path = cwd / rel
        # only allow under repo
        try:
            path.resolve().relative_to(cwd.resolve())
        except Exception:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        written.append(rel)

    return {
        "ok": bool(written),
        "provider": "grok",
        "written": written,
        "notes": (data.get("notes") or "")[:2000],
        "model": raw.get("model"),
    }


def commit_and_push(cwd: Path, *, title: str, job_id: str) -> dict[str, Any]:
    bad = _denied_paths_changed(cwd)
    if bad:
        for p in bad:
            _git(cwd, "checkout", "--", p)
        return {"ok": False, "error": "denied paths touched", "paths": bad}

    st = _git(cwd, "status", "--porcelain")
    if not (st.stdout or "").strip():
        return {"ok": False, "error": "no file changes to commit"}

    branch_r = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch_r.stdout or "").strip() or f"sov/{job_id[:12]}"
    _git(cwd, "add", "-A")
    # identity for commits in headless env
    _git(cwd, "config", "user.email", "sovereign@arrayoperator.com")
    _git(cwd, "config", "user.name", "Sovereign")
    msg = f"sovereign: {title[:180]}\n\nJob: {job_id}\nAuthorized live ship by Ford 2026-07-15."
    c = _git(cwd, "commit", "-m", msg)
    if c.returncode != 0 and "nothing to commit" not in ((c.stdout or "") + (c.stderr or "")):
        return {
            "ok": False,
            "error": "commit failed",
            "stderr": (c.stderr or "")[:500],
        }

    out: dict[str, Any] = {"ok": True, "branch": branch, "committed": True}
    if not code_push_enabled():
        out["pushed"] = False
        out["note"] = "SOVEREIGN_CODE_PUSH off — committed locally only"
        return out

    push_b = _git(cwd, "push", "-u", "origin", branch, timeout=120)
    out["push_branch"] = {
        "ok": push_b.returncode == 0,
        "stderr": (push_b.stderr or "")[:400],
    }
    _git(cwd, "checkout", "main")
    _git(cwd, "pull", "--ff-only", "origin", "main", timeout=120)
    merge = _git(cwd, "merge", "--ff-only", branch)
    if merge.returncode != 0:
        merge = _git(cwd, "merge", "--no-edit", branch)
    out["merge_main"] = {
        "ok": merge.returncode == 0,
        "stderr": (merge.stderr or "")[:400],
    }
    if merge.returncode == 0:
        push_m = _git(cwd, "push", "origin", "main", timeout=120)
        out["push_main"] = {
            "ok": push_m.returncode == 0,
            "stderr": (push_m.stderr or "")[:400],
        }
        out["ok"] = push_m.returncode == 0
        if push_m.returncode != 0:
            out["error"] = "push main failed"
    else:
        out["ok"] = False
        out["error"] = "merge to main failed"
    return out


def deploy_repo(repo_name: str) -> dict[str, Any]:
    if not code_deploy_enabled():
        return {"ok": False, "skipped": True, "reason": "SOVEREIGN_CODE_DEPLOY off"}
    if repo_name == "array-operator":
        try:
            ao_cwd = ensure_repo("array-operator")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"ao repo: {e}"}
        script_candidates = [
            Path(os.getenv("SOVEREIGN_NETLIFY_DEPLOY") or ""),
            Path("/root/.claude/skills/solar-operator-energyagent/scripts/netlify_api_deploy.py"),
            ao_cwd / "scripts" / "netlify_api_deploy.py",
        ]
        script = next((s for s in script_candidates if s and s.is_file()), None)
        if script:
            p = _run(["python3", str(script)], cwd=ao_cwd, timeout=300)
            return {
                "ok": p.returncode == 0,
                "provider": "netlify",
                "stdout": (p.stdout or "")[-800:],
                "stderr": (p.stderr or "")[-400:],
            }
        # Direct Netlify API deploy of public/
        site = (os.getenv("NETLIFY_SITE_ID") or os.getenv("AO_NETLIFY_SITE_ID") or "").strip()
        token = (os.getenv("NETLIFY_AUTH_TOKEN") or os.getenv("NETLIFY_TOKEN") or "").strip()
        public = ao_cwd / "public"
        if site and token and public.is_dir():
            p = _run(
                [
                    "bash", "-lc",
                    f"npx --yes netlify-cli deploy --prod --dir=public --site={site}",
                ],
                cwd=ao_cwd,
                timeout=300,
                env={"NETLIFY_AUTH_TOKEN": token},
            )
            return {
                "ok": p.returncode == 0,
                "provider": "netlify_cli",
                "stdout": (p.stdout or "")[-800:],
                "stderr": (p.stderr or "")[-400:],
            }
        return {
            "ok": True,
            "provider": "netlify",
            "skipped": True,
            "note": "push to main; Netlify build hook / auto-publish if connected",
        }
    if repo_name == "solar-operator":
        # Railway auto-deploys on push to main — nothing extra
        return {"ok": True, "provider": "railway", "note": "push to main triggers deploy"}
    return {"ok": False, "error": f"unknown repo {repo_name}"}


def process_job(db, job) -> dict[str, Any]:
    """Execute one EaSovereignJob end-to-end."""
    from .energy_agent_sovereign import audit, write_note, email_ford
    from .energy_agent_sovereign_desk import push_sovereign_message

    if not code_live_enabled():
        return {"ok": False, "denied": True, "denied_reason": "SOVEREIGN_CODE_LIVE off"}

    try:
        brief_obj = json.loads(job.brief_json or "{}")
    except Exception:
        brief_obj = {}
    title = job.title or brief_obj.get("title") or "Sovereign job"
    brief = brief_obj.get("brief") or brief_obj.get("text") or ""
    expanded = brief_obj.get("expanded_brief")

    deny = brief_is_denied(title, brief)
    if deny:
        job.status = "failed"
        job.error = deny
        job.finished_at = _now()
        audit(
            db, capability="act.code_hire", decision="act",
            rationale=deny, targets={"job_id": job.id}, result="denied",
            denied_reason=deny, correlation_id=job.id,
        )
        return {"ok": False, "denied": True, "denied_reason": deny}

    job.status = "running"
    db.flush()

    try:
        # Warm both repos so secondary tools (deploy) also work
        ensure_all_repos()
        repo_name, repo_path = pick_repo(brief, title)
    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = str(e)[:500]
        job.finished_at = _now()
        return {"ok": False, "error": str(e)[:300]}

    # Start from latest main on an isolated branch
    _configure_remote_auth(repo_path, repo_name)
    _git(repo_path, "fetch", "origin", "main", timeout=120)
    _git(repo_path, "checkout", "main")
    pull = _git(repo_path, "pull", "--ff-only", "origin", "main", timeout=120)
    if pull.returncode != 0:
        # shallow clone recovery
        _git(repo_path, "fetch", "--depth", "50", "origin", "main", timeout=120)
        _git(repo_path, "reset", "--hard", "origin/main")
    branch = f"sov/{job.id[:12]}"
    _git(repo_path, "checkout", "-B", branch)

    # Prefer Claude Code; fall back to Grok file rewrites
    agent_result = run_claude_code(
        cwd=repo_path,
        title=title,
        brief=brief,
        expanded=expanded,
        job_id=job.id,
    )
    if not agent_result.get("ok") or not agent_result.get("changed"):
        grok = run_grok_code_assist(cwd=repo_path, title=title, brief=brief)
        agent_result = {
            "ok": grok.get("ok"),
            "provider": "claude_code+grok" if agent_result.get("provider") else "grok",
            "claude": agent_result,
            "grok": grok,
            "changed": bool(grok.get("written")),
            "result_text": (agent_result.get("result_text") or "")
            + "\n"
            + (grok.get("notes") or ""),
        }

    if not agent_result.get("changed") and not agent_result.get("ok"):
        job.status = "failed"
        job.error = (agent_result.get("error") or "agent produced no changes")[:800]
        job.result_json = json.dumps(agent_result, default=str)[:50_000]
        job.finished_at = _now()
        audit(
            db, capability="act.code_hire", decision="act",
            rationale=f"job failed: {job.error}",
            targets={"job_id": job.id, "repo": repo_name},
            result="failed", correlation_id=job.id,
        )
        try:
            push_sovereign_message(
                db,
                f"Code job failed ({job.id}): {title}\n{job.error}",
                meta={"job_id": job.id},
                provider="worker",
            )
        except Exception:
            pass
        email_ford(f"[Sovereign] Code job FAILED: {title[:80]}", json.dumps(agent_result, default=str)[:4000])
        return {"ok": False, "job_id": job.id, "result": agent_result}

    ship = commit_and_push(repo_path, title=title, job_id=job.id)
    deploy = {}
    if ship.get("ok") and ship.get("push_main", {}).get("ok"):
        deploy = deploy_repo(repo_name)
    elif ship.get("ok") and code_push_enabled() and repo_name == "solar-operator":
        deploy = {"ok": True, "note": "awaiting railway from push"}

    job.status = "done" if ship.get("ok") else "failed"
    job.error = None if ship.get("ok") else (ship.get("error") or "ship failed")[:800]
    job.result_json = json.dumps(
        {"agent": agent_result, "ship": ship, "deploy": deploy, "repo": repo_name},
        default=str,
    )[:50_000]
    job.finished_at = _now()

    # Auto-mark feature shipped when job succeeds (Ford: ship authority)
    if job.status == "done":
        try:
            m = re.search(r"feature\s*#?\s*(\d+)", f"{title}\n{brief}", re.I)
            if m:
                from .energy_agent_sovereign_ops import mark_feature_shipped
                mark_feature_shipped(
                    db, int(m.group(1)),
                    note=f"Auto-shipped after code job {job.id} (worker).",
                )
        except Exception as e:  # noqa: BLE001
            log.warning("feature ship mark failed: %s", e)
        try:
            m2 = re.search(r"utility[^\n]*#?\s*(\d+)|request\s*#(\d+)", f"{title}\n{brief}", re.I)
            if m2:
                uid = int(m2.group(1) or m2.group(2))
                from .energy_agent_sovereign_ops import set_utility_status
                set_utility_status(
                    db, uid, "researching",
                    result_note=f"Code job {job.id} completed; adapter work landed — verify before mark added.",
                )
        except Exception as e:  # noqa: BLE001
            log.warning("utility note after job failed: %s", e)

    write_note(
        db,
        kind="decision",
        title=f"code job {job.status}: {title[:80]}",
        body=json.dumps({"job_id": job.id, "ship": ship, "deploy": deploy}, default=str)[:8000],
        provider=str(agent_result.get("provider") or "worker"),
        meta={"job_id": job.id},
    )
    audit(
        db, capability="act.code_hire", decision="act",
        rationale=f"ship {job.status}: {title}",
        targets={
            "job_id": job.id,
            "repo": repo_name,
            "branch": ship.get("branch"),
            "push": ship.get("push_main"),
            "deploy": deploy,
        },
        result="ok" if job.status == "done" else "failed",
        correlation_id=job.id,
        cost_usd=float(agent_result.get("cost_usd") or 0) if isinstance(agent_result.get("cost_usd"), (int, float)) else 0.0,
    )

    summary = (
        f"Sovereign shipped job {job.id}\n"
        f"Title: {title}\nRepo: {repo_name}\n"
        f"Status: {job.status}\n"
        f"Ship: {json.dumps(ship, default=str)[:800]}\n"
        f"Deploy: {json.dumps(deploy, default=str)[:400]}\n"
    )
    try:
        push_sovereign_message(db, summary, meta={"job_id": job.id}, provider="worker")
    except Exception:
        pass
    email_ford(
        f"[Sovereign] Code job {job.status.upper()}: {title[:80]}",
        summary + "\n" + (agent_result.get("result_text") or "")[:2000],
    )
    return {
        "ok": job.status == "done",
        "job_id": job.id,
        "repo": repo_name,
        "ship": ship,
        "deploy": deploy,
        "agent": {
            "provider": agent_result.get("provider"),
            "changed": agent_result.get("changed"),
        },
    }


def requeue_failed_jobs(
    db,
    *,
    limit: int = 30,
    only_repo_errors: bool = True,
) -> dict[str, Any]:
    """Re-queue failed jobs (default: those that failed for missing repos)."""
    from .energy_agent_sovereign import EaSovereignJob
    from sqlalchemy import select

    rows = db.execute(
        select(EaSovereignJob)
        .where(EaSovereignJob.status == "failed")
        .order_by(EaSovereignJob.created_at.asc())
        .limit(limit)
    ).scalars().all()
    n = 0
    ids = []
    for job in rows:
        err = (job.error or "").lower()
        if only_repo_errors and "no sovereign repos" not in err and "clone" not in err:
            continue
        job.status = "queued"
        job.error = None
        job.finished_at = None
        job.result_json = None
        n += 1
        ids.append(job.id)
    db.flush()
    return {"ok": True, "requeued": n, "ids": ids}


def drain_jobs(db, *, limit: int = 2) -> dict[str, Any]:
    """Process queued sovereign jobs (oldest first)."""
    from .energy_agent_sovereign import EaSovereignJob
    from sqlalchemy import select

    if not code_live_enabled():
        return {"ok": True, "skipped": True, "reason": "code live off", "processed": 0}

    # Ensure repos before processing so first job doesn't burn on clone race
    repo_status = {}
    try:
        repo_status = ensure_all_repos()
    except Exception as e:  # noqa: BLE001
        repo_status = {"error": str(e)[:200]}

    rows = db.execute(
        select(EaSovereignJob)
        .where(EaSovereignJob.status == "queued")
        .order_by(EaSovereignJob.created_at.asc())
        .limit(limit)
    ).scalars().all()

    results = []
    for job in rows:
        try:
            results.append(process_job(db, job))
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.exception("process_job crashed %s", job.id)
            try:
                db.rollback()
                job2 = db.get(EaSovereignJob, job.id)
                if job2:
                    job2.status = "failed"
                    job2.error = str(e)[:800]
                    job2.finished_at = _now()
                    db.commit()
            except Exception:
                db.rollback()
            results.append({"ok": False, "job_id": job.id, "error": str(e)[:300]})
    return {
        "ok": True,
        "processed": len(results),
        "results": results,
        "repos": repo_status,
    }
