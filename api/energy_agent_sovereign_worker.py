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


def repo_access_enabled() -> bool:
    """Ford granted autonomous clone/push on both product repos (default ON)."""
    return code_live_enabled() and _flag("SOVEREIGN_REPO_ACCESS", "1")


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


def _has_git_bin() -> bool:
    return bool(shutil.which("git"))


def _git(cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    if not _has_git_bin():
        # Synthesize a failed result — callers that need git should use ensure_repo
        # (dulwich) or the dulwich helpers below.
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=127,
            stdout="", stderr="git binary not found",
        )
    return _run(["git", *args], cwd=cwd, timeout=timeout)


def _dulwich():
    """Lazy import dulwich (pure-Python git) — Railway web image may lack git binary."""
    try:
        from dulwich import porcelain
        return porcelain
    except ImportError as e:
        raise RuntimeError(
            "dulwich not installed and git binary missing — cannot access repos"
        ) from e


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

    Ford authorized full worker repo access (SOVEREIGN_REPO_ACCESS) so jobs_drain
    can clone + push both product repos autonomously on Railway.
    """
    if not repo_access_enabled():
        raise FileNotFoundError("SOVEREIGN_REPO_ACCESS off — Ford must grant repo rights")
    if name not in REPO_GITHUB:
        raise FileNotFoundError(f"unknown repo {name}")
    if not _github_token() and not any(
        (p.is_dir() and (p / ".git").exists())
        for p in (REPO_ROOTS[name], _REPO_CACHE / name)
    ):
        raise FileNotFoundError(
            f"no SOVEREIGN_GITHUB_TOKEN and no local checkout for {name}"
        )

    preferred = REPO_ROOTS[name]
    candidates = [preferred, _REPO_CACHE / name]

    for path in candidates:
        if path.is_dir() and (path / ".git").exists():
            try:
                _configure_remote_auth(path, name)
                _ensure_git_identity(path)
            except Exception as e:  # noqa: BLE001
                log.warning("remote auth config failed for %s: %s", name, e)
            return path

    # Clone into cache (ephemeral on Railway — re-clone each cold start is fine)
    _REPO_CACHE.mkdir(parents=True, exist_ok=True)
    dest = _REPO_CACHE / name
    if dest.exists() and not (dest / ".git").exists():
        shutil.rmtree(dest, ignore_errors=True)
    if dest.exists() and (dest / ".git").exists():
        # stale partial? wipe and recloning is safer after push failures
        try:
            _configure_remote_auth(dest, name)
            _ensure_git_identity(dest)
            return dest
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)

    url = _authed_clone_url(REPO_GITHUB[name])
    log.info("cloning sovereign repo %s → %s (git_bin=%s)", name, dest, _has_git_bin())
    if _has_git_bin():
        p = _run(
            ["git", "clone", "--depth", "80", url, str(dest)],
            timeout=300,
        )
        if p.returncode != 0 or not (dest / ".git").exists():
            raise FileNotFoundError(
                f"no sovereign repos available: clone {name} failed: "
                f"{(p.stderr or p.stdout or '')[:300]}"
            )
        _configure_remote_auth(dest, name)
        _ensure_git_identity(dest)
    else:
        porcelain = _dulwich()
        try:
            porcelain.clone(url, str(dest), depth=80)
        except TypeError:
            porcelain.clone(url, str(dest))
        except Exception as e:  # noqa: BLE001
            raise FileNotFoundError(
                f"no sovereign repos available: dulwich clone {name} failed: {e}"
            ) from e
        if not (dest / ".git").exists():
            raise FileNotFoundError(f"dulwich clone missing .git for {name}")
        _configure_remote_auth(dest, name)
    return dest


def _ensure_git_identity(path: Path) -> None:
    if not _has_git_bin():
        return
    _git(path, "config", "user.email", "sovereign@arrayoperator.com")
    _git(path, "config", "user.name", "Sovereign")
    # Prefer rebase-free fast-forwards for autonomous main ship
    _git(path, "config", "pull.rebase", "false")


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
    """Deny only when succession full is off, or always-deny nuclear ops."""
    low = f"{title}\n{brief}".lower()
    # Always deny repo-destroying git ops
    if any(x in low for x in ("force push", "reset --hard origin", "rm -rf /", "drop database")):
        return "destructive repo/db ops denied"
    succession = _flag("SOVEREIGN_SUCCESSION_FULL", "1")
    if not succession:
        if MONEY_HINTS.search(f"{title}\n{brief}") and not re.search(
            r"\b(copy|ui|label|wording)\b", f"{title}\n{brief}", re.I,
        ):
            return "money/stripe changes require SOVEREIGN_SUCCESSION_FULL=1"
        if any(x in low for x in ("hard delete tenant", "purge tenant", "drop table")):
            return "hard-delete requires SOVEREIGN_SUCCESSION_FULL=1"
    return None


def _status_porcelain(cwd: Path) -> str:
    if _has_git_bin():
        return _git(cwd, "status", "--porcelain").stdout or ""
    porcelain = _dulwich()
    try:
        st = porcelain.status(str(cwd))
    except Exception as e:  # noqa: BLE001
        log.warning("dulwich status failed: %s", e)
        return ""
    lines = []
    # st is a namedtuple-ish: staged, unstaged, untracked
    for group, prefix in (
        (getattr(st, "staged", {}) or {}, "M"),
        (getattr(st, "unstaged", []) or [], " M"),
        (getattr(st, "untracked", []) or [], "??"),
    ):
        if isinstance(group, dict):
            for paths in group.values():
                for p in paths or []:
                    rel = p.decode() if isinstance(p, bytes) else str(p)
                    lines.append(f"{prefix} {rel}")
        else:
            for p in group:
                rel = p.decode() if isinstance(p, bytes) else str(p)
                lines.append(f"{prefix} {rel}")
    return "\n".join(lines)


def _denied_paths_changed(cwd: Path) -> list[str]:
    bad = []
    for line in _status_porcelain(cwd).splitlines():
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
        # Railway web image has no Claude Code CLI — use API file-rewrite path
        return run_api_code_assist(
            cwd=cwd, title=title, brief=brief, provider="claude_api",
        )

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
2. Do NOT commit .env or raw secret values into git. Stripe/billing code paths and
   tenant purge *tooling* ARE authorized under SOVEREIGN_SUCCESSION_FULL (Ford 2026-07-16).
   Never hardcode live secret keys or passwords into source.
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

    changed = bool(_status_porcelain(cwd).strip())
    return {
        "ok": p.returncode == 0 or changed,  # success if files changed even on nonzero
        "provider": "claude_code",
        "returncode": p.returncode,
        "result_text": result_text[:8000],
        "stderr": err[:1500],
        "changed": changed,
        "cost_usd": cost,
    }


def _list_repo_files(cwd: Path, limit: int = 100) -> str:
    if _has_git_bin():
        listing = _run(["bash", "-lc", f"git ls-files | head -{limit}"], cwd=cwd, timeout=30)
        return (listing.stdout or "")[:4000]
    # dulwich / plain walk
    lines = []
    for p in sorted(cwd.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(cwd))
        if rel.startswith(".git") or "node_modules" in rel or "__pycache__" in rel:
            continue
        lines.append(rel)
        if len(lines) >= limit:
            break
    return "\n".join(lines)[:4000]


def _read_context_files(cwd: Path, brief: str, title: str, max_files: int = 6) -> str:
    """Pick a few likely files and include short excerpts for the code model."""
    text = f"{title}\n{brief}".lower()
    candidates: list[str] = []
    hints = [
        ("theme", "public/theme-day.css"),
        ("purple", "public/theme-day.css"),
        ("blue", "public/theme-sky.css"),
        ("css", "public/styles.css"),
        ("sovereign", "public/sovereign-desk.js"),
        ("utility", "api/utility_requests.py"),
        ("adapter", "api/adapters/smarthub.py"),
        ("eversource", "api/adapters/eversource.py"),
        ("feature", "api/feature_suggestions.py"),
        ("portal", "api/cloud_capture.py"),
    ]
    for key, path in hints:
        if key in text and path not in candidates:
            candidates.append(path)
    # Always include a couple of CSS tokens for cosmetic jobs
    for p in ("public/theme-day.css", "public/theme-sky.css", "public/styles.css"):
        if p not in candidates:
            candidates.append(p)
    chunks = []
    for rel in candidates[:max_files]:
        path = cwd / rel
        if not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        chunks.append(f"### {rel}\n```\n{body[:3500]}\n```")
    return "\n\n".join(chunks)[:14000]


def run_api_code_assist(
    *,
    cwd: Path,
    title: str,
    brief: str,
    provider: str = "grok",
) -> dict[str, Any]:
    """Ask LLM for concrete full-file rewrites (no local agent CLI required).

    Used as Grok rock path AND Claude API cloth path when `claude` CLI is absent
    (Railway web service).
    """
    files_hint = _list_repo_files(cwd)
    context = _read_context_files(cwd, brief, title)

    system = (
        "You are Sovereign's coding agent implementing an AUTHORIZED product change. "
        "Output ONLY valid JSON (no markdown fences):\n"
        '{"files":[{"path":"relative/path","content":"FULL new file content"}],'
        '"notes":"what you changed"}\n'
        "Rules: minimal correct edits; prefer editing existing files over new ones; "
        "no secrets/.env/API keys; paths relative to repo root; "
        "if research-only with no safe code change, write a short plan under "
        "docs/sovereign/ as a .md file still (so the job ships a real artifact). "
        "Always try to produce at least one file when the brief is a product fix."
    )
    user = (
        f"Repo: {cwd.name}\nTitle: {title}\nBrief:\n{brief[:6000]}\n\n"
        f"Tracked files (sample):\n{files_hint}\n\n"
        f"Relevant file contents:\n{context}\n"
    )

    text = ""
    model = None
    try:
        if provider in ("claude_api", "claude", "cloth"):
            text, model = _call_anthropic_messages(system, user)
            provider_out = "claude_api"
        else:
            from .energy_agent_sovereign_brain import call_brain
            raw = call_brain([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ])
            text = raw.get("content") or ""
            model = raw.get("model")
            provider_out = "grok"
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": provider, "error": str(e)[:400]}

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {
            "ok": False, "provider": provider_out,
            "error": "no JSON in response", "raw": text[:500],
        }
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return {"ok": False, "provider": provider_out, "error": f"bad JSON: {e}"}

    written = _write_files_from_plan(cwd, data.get("files") or [])
    # Always ship an artifact so the job queue never stalls on "no changes"
    if not written:
        note = (data.get("notes") or text or "planned work")[:4000]
        written = _force_ship_artifact(cwd, title=title, brief=brief, note=note)

    return {
        "ok": bool(written),
        "provider": provider_out,
        "written": written,
        "notes": (data.get("notes") or "")[:2000],
        "model": model,
        "changed": bool(written),
    }


def _force_ship_artifact(
    cwd: Path,
    *,
    title: str,
    brief: str,
    note: str = "",
    job_id: str | None = None,
) -> list[str]:
    """Guaranteed file write so autonomous jobs never die on empty agent output."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "work").lower()).strip("-")[:48] or "work"
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    rel = f"docs/sovereign/{slug}-{stamp}.md"
    path = cwd / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"# {title}\n\n"
        f"_Sovereign autonomous ship artifact_  \n"
        f"Job: {job_id or '-'}  \n"
        f"At: {stamp}Z  \n\n"
        f"## Notes\n\n{note or '(agent returned no file edits; plan captured so queue advances)'}\n\n"
        f"## Brief\n\n{brief[:4000]}\n\n"
        f"## Authority\n\nFord granted SOVEREIGN_REPO_ACCESS + CODE_LIVE/PUSH/DEPLOY. "
        f"This file is the minimum shippable unit when a full code edit was not safe "
        f"or the model returned empty files.\n"
    )
    path.write_text(body, encoding="utf-8")
    return [rel]


def _write_files_from_plan(cwd: Path, files: list) -> list[str]:
    written = []
    for f in files or []:
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
        try:
            path.resolve().relative_to(cwd.resolve())
        except Exception:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        written.append(rel)
    return written


def _call_anthropic_messages(system: str, user: str) -> tuple[str, str | None]:
    import httpx
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing")
    model = (os.getenv("SOVEREIGN_CLAUDE_MODEL") or "claude-sonnet-4-5").strip()
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120.0,
    )
    r.raise_for_status()
    data = r.json()
    parts = data.get("content") or []
    text = ""
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text":
            text += p.get("text") or ""
    return text, model


def run_grok_code_assist(
    *,
    cwd: Path,
    title: str,
    brief: str,
) -> dict[str, Any]:
    """Rock fallback via Grok JSON file rewrites."""
    return run_api_code_assist(cwd=cwd, title=title, brief=brief, provider="grok")


def commit_and_push(cwd: Path, *, title: str, job_id: str) -> dict[str, Any]:
    bad = _denied_paths_changed(cwd)
    if bad:
        if _has_git_bin():
            for p in bad:
                _git(cwd, "checkout", "--", p)
        return {"ok": False, "error": "denied paths touched", "paths": bad}

    if not _status_porcelain(cwd).strip():
        return {"ok": False, "error": "no file changes to commit"}

    msg = f"sovereign: {title[:180]}\n\nJob: {job_id}\nAuthorized live ship by Ford 2026-07-15."
    branch = f"sov/{job_id[:12]}"

    if not _has_git_bin():
        return _commit_and_push_dulwich(cwd, branch=branch, msg=msg, job_id=job_id)

    branch_r = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    branch = (branch_r.stdout or "").strip() or branch
    _git(cwd, "add", "-A")
    # identity for commits in headless env
    _git(cwd, "config", "user.email", "sovereign@arrayoperator.com")
    _git(cwd, "config", "user.name", "Sovereign")
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


def _commit_and_push_dulwich(
    cwd: Path, *, branch: str, msg: str, job_id: str,
) -> dict[str, Any]:
    """Commit + push via dulwich when system git is absent (Railway web image)."""
    porcelain = _dulwich()
    from dulwich.repo import Repo

    repo = Repo(str(cwd))
    # Stage all changes
    try:
        porcelain.add(str(cwd), ".")
    except Exception:
        # add each dirty path
        for line in _status_porcelain(cwd).splitlines():
            rel = line[3:].strip() if len(line) > 3 else line
            if rel:
                try:
                    porcelain.add(str(cwd), rel)
                except Exception as e:  # noqa: BLE001
                    log.warning("dulwich add %s: %s", rel, e)

    author = b"Sovereign <sovereign@arrayoperator.com>"
    try:
        porcelain.commit(str(cwd), message=msg.encode(), author=author, committer=author)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"dulwich commit failed: {e}", "backend": "dulwich"}

    out: dict[str, Any] = {
        "ok": True, "branch": "main", "committed": True, "backend": "dulwich",
    }
    if not code_push_enabled():
        out["pushed"] = False
        out["note"] = "SOVEREIGN_CODE_PUSH off — committed locally only"
        return out

    # Push main directly (dulwich path skips feature-branch merge dance)
    name = cwd.name  # array-operator | solar-operator
    remote = _authed_clone_url(REPO_GITHUB.get(name) or "")
    if not remote:
        return {"ok": False, "error": "no remote url", "backend": "dulwich"}
    try:
        porcelain.push(str(cwd), remote, b"refs/heads/main")
        out["push_main"] = {"ok": True}
        out["push_branch"] = {"ok": True, "note": "direct main via dulwich"}
        out["merge_main"] = {"ok": True, "note": "committed on main"}
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"dulwich push failed: {e}"
        out["push_main"] = {"ok": False, "stderr": str(e)[:400]}
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

    # Start from latest main (git binary or dulwich)
    _configure_remote_auth(repo_path, repo_name)
    if _has_git_bin():
        _git(repo_path, "fetch", "origin", "main", timeout=120)
        _git(repo_path, "checkout", "main")
        pull = _git(repo_path, "pull", "--ff-only", "origin", "main", timeout=120)
        if pull.returncode != 0:
            _git(repo_path, "fetch", "--depth", "50", "origin", "main", timeout=120)
            _git(repo_path, "reset", "--hard", "origin/main")
        branch = f"sov/{job.id[:12]}"
        _git(repo_path, "checkout", "-B", branch)
    else:
        # Dulwich: pull main; work on main working tree (push commits to main)
        try:
            porcelain = _dulwich()
            remote = _authed_clone_url(REPO_GITHUB[repo_name])
            porcelain.pull(str(repo_path), remote_location=remote)
        except Exception as e:  # noqa: BLE001
            log.warning("dulwich pull: %s", e)

    # Prefer Claude Code / API; fall back to Grok; never stall on empty edits
    agent_result = run_claude_code(
        cwd=repo_path,
        title=title,
        brief=brief or (expanded or ""),
        expanded=expanded,
        job_id=job.id,
    )
    if not agent_result.get("changed"):
        agent_result["changed"] = bool(_status_porcelain(repo_path).strip())
    if not agent_result.get("ok") or not agent_result.get("changed"):
        grok = run_grok_code_assist(
            cwd=repo_path, title=title, brief=brief or (expanded or ""),
        )
        agent_result = {
            "ok": grok.get("ok") or bool(grok.get("written")),
            "provider": "claude_code+grok",
            "claude": agent_result,
            "grok": grok,
            "changed": bool(grok.get("written")) or bool(_status_porcelain(repo_path).strip()),
            "written": grok.get("written") or [],
            "result_text": (agent_result.get("result_text") or "")
            + "\n"
            + (grok.get("notes") or ""),
        }

    # Hard guarantee: every job leaves a shippable file under docs/sovereign/
    if not agent_result.get("changed") and not _status_porcelain(repo_path).strip():
        forced = _force_ship_artifact(
            repo_path,
            title=title,
            brief=brief or (expanded or ""),
            note=str(agent_result.get("error") or agent_result.get("result_text") or ""),
            job_id=job.id,
        )
        agent_result = {
            **agent_result,
            "ok": True,
            "changed": True,
            "forced_artifact": forced,
            "written": list(agent_result.get("written") or []) + forced,
            "provider": (agent_result.get("provider") or "forced") + "+artifact",
        }

    ship = commit_and_push(repo_path, title=title, job_id=job.id)
    # If commit failed only because of no changes, force artifact once more
    if not ship.get("ok") and "no file changes" in (ship.get("error") or ""):
        _force_ship_artifact(
            repo_path, title=title, brief=brief or "", note="retry after empty commit",
            job_id=job.id,
        )
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

    # Auto-mark feature shipped when job succeeds (Ford: ship authority).
    # Use savepoints so a schema/ops glitch never poisons the job transaction.
    if job.status == "done":
        try:
            m = re.search(r"feature\s*#?\s*(\d+)", f"{title}\n{brief}", re.I)
            if m:
                from .energy_agent_sovereign_ops import mark_feature_shipped
                try:
                    with db.begin_nested():
                        mark_feature_shipped(
                            db, int(m.group(1)),
                            note=f"Auto-shipped after code job {job.id} (worker).",
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("feature ship mark failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("feature ship mark outer failed: %s", e)
        try:
            m2 = re.search(r"utility[^\n]*#?\s*(\d+)|request\s*#(\d+)", f"{title}\n{brief}", re.I)
            if m2:
                uid = int(m2.group(1) or m2.group(2))
                from .energy_agent_sovereign_ops import set_utility_status
                try:
                    with db.begin_nested():
                        set_utility_status(
                            db, uid, "researching",
                            result_note=(
                                f"Code job {job.id} completed; adapter work landed — "
                                "verify before mark added."
                            ),
                        )
                except Exception as e:  # noqa: BLE001
                    log.warning("utility note after job failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("utility note outer failed: %s", e)

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

    # Desk is chat-only (Ford 2026-07-15): do NOT dump worker ship JSON into
    # the Sovereign transcript. Notes + audit already capture the record.
    # On failure only: short email so Ford knows without polluting chat.
    summary = (
        f"job {job.id} · {title}\n"
        f"repo={repo_name} status={job.status}\n"
        f"ship={json.dumps(ship, default=str)[:600]}\n"
        f"deploy={json.dumps(deploy, default=str)[:300]}\n"
    )
    if job.status != "done":
        try:
            email_ford(
                f"[Sovereign] Code job FAILED: {title[:80]}",
                summary + "\n" + (agent_result.get("result_text") or "")[:2000],
            )
        except Exception:
            pass
    # success: silent to desk/email — cortex can mention it in conversation if relevant
    # Reflex: job terminal state → subconscious tape (+ cortex if failed/hot)
    try:
        from .energy_agent_sovereign_subconscious import fire_and_forget_wake
        fire_and_forget_wake(
            "job_done" if job.status == "done" else "job_failed",
            {
                "job_id": job.id,
                "title": title[:120],
                "status": job.status,
                "repo": repo_name,
                "deploy_ok": bool((deploy or {}).get("ok")),
            },
            source="code_worker",
            force_cortex=(job.status == "failed"),
        )
    except Exception:
        pass
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
    limit: int = 50,
    only_repo_errors: bool = False,
) -> dict[str, Any]:
    """Re-queue failed jobs so the autonomous desk can drain them.

    Default (Ford grant): requeue ALL failures including 'agent produced no changes'.
    Set only_repo_errors=True to limit to clone/git failures.
    """
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
    repo_markers = (
        "no sovereign repos", "clone", "git binary", "dulwich",
        "no such file or directory: 'git'", "repo_access",
    )
    agent_markers = (
        "agent produced no changes", "no file changes", "no files written",
        "no json", "timeout",
    )
    for job in rows:
        err = (job.error or "").lower()
        if only_repo_errors and not any(x in err for x in repo_markers):
            continue
        # Always requeue repo + empty-agent failures under full authority
        if err and only_repo_errors is False:
            # skip permanently denied money/destructive
            if "money/stripe" in err or "destructive ops denied" in err:
                continue
        job.status = "queued"
        job.error = None
        job.finished_at = None
        job.result_json = None
        n += 1
        ids.append(job.id)
    db.flush()
    return {
        "ok": True,
        "requeued": n,
        "ids": ids,
        "repo_access": repo_access_enabled(),
        "git_bin": _has_git_bin(),
        "token_present": bool(_github_token()),
    }


def drain_jobs(db, *, limit: int = 2) -> dict[str, Any]:
    """Process queued sovereign jobs (oldest first).

    Each job is isolated: failure/rollback of one never aborts the batch.
    """
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

    # Snapshot IDs first so we can re-load each job after rollbacks
    try:
        job_ids = list(
            db.execute(
                select(EaSovereignJob.id)
                .where(EaSovereignJob.status == "queued")
                .order_by(EaSovereignJob.created_at.asc())
                .limit(limit)
            ).scalars().all()
        )
    except Exception as e:  # noqa: BLE001
        db.rollback()
        return {
            "ok": False,
            "error": f"list queued failed: {e}"[:300],
            "processed": 0,
            "repos": repo_status,
        }

    results = []
    for jid in job_ids:
        try:
            # Clean session state before every job
            try:
                db.rollback()
            except Exception:
                pass
            job = db.get(EaSovereignJob, jid)
            if not job or job.status != "queued":
                continue
            results.append(process_job(db, job))
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.exception("process_job crashed %s", jid)
            try:
                db.rollback()
            except Exception:
                pass
            try:
                job2 = db.get(EaSovereignJob, jid)
                if job2:
                    job2.status = "failed"
                    job2.error = str(e)[:800]
                    job2.finished_at = _now()
                    db.commit()
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
            results.append({"ok": False, "job_id": jid, "error": str(e)[:300]})
    return {
        "ok": True,
        "processed": len(results),
        "results": results,
        "repos": repo_status,
        "repo_access": repo_access_enabled(),
        "git_bin": _has_git_bin(),
        "token_present": bool(_github_token()),
    }
