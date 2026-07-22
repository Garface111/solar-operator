"""Sovereign Chamber L2 — always-on false-real Array Operator URL.

Deploys sandbox (or baseline) AO ``public/`` as a Netlify **branch deploy**
on the existing production site. Never publishes arrayoperator.com.

Stable URL (default branch name ``chamber``):
  https://chamber--array-operator-ea.netlify.app

See docs/sovereign/ROCKET_ENGINE.md (L2).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("energy_agent.sovereign.chamber")

_PKG = Path(__file__).resolve().parent
SO_ROOT = Path(os.getenv("SOVEREIGN_REPO_ROOT") or _PKG.parent).resolve()
AO_ROOT = Path(
    os.getenv("ARRAY_OPERATOR_ROOT")
    or os.getenv("SOVEREIGN_AO_ROOT")
    or (SO_ROOT.parent / "array-operator")
).resolve()

DEFAULT_CHAMBER_URL = "https://chamber--array-operator-ea.netlify.app"
CHAMBER_META_KEY = "sovereign_chamber"


def chamber_enabled() -> bool:
    return (os.getenv("SOVEREIGN_CHAMBER_DEPLOY", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def default_chamber_url() -> str:
    return (
        os.getenv("SOVEREIGN_CHAMBER_URL")
        or DEFAULT_CHAMBER_URL
    ).rstrip("/")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_deploy_script() -> Path | None:
    candidates = [
        Path(os.getenv("CHAMBER_DEPLOY_SCRIPT") or ""),
        AO_ROOT / "scripts" / "chamber_deploy_dir.py",
        Path("/root/array-operator/scripts/chamber_deploy_dir.py"),
        SO_ROOT.parent / "array-operator" / "scripts" / "chamber_deploy_dir.py",
    ]
    for p in candidates:
        if p and p.is_file():
            return p
    return None


def resolve_public_dir(
    *,
    run_id: str | None = None,
    public_dir: str | Path | None = None,
) -> Path | None:
    """Prefer sandbox AO public/, then live AO public/."""
    if public_dir:
        p = Path(public_dir).expanduser().resolve()
        if p.is_dir():
            return p
    if run_id:
        try:
            from .energy_agent_sovereign_mind_sandbox import sandbox_repo_path, run_dir

            for cand in (
                sandbox_repo_path(run_id, "array-operator") / "public",
                run_dir(run_id) / "array-operator" / "public",
            ):
                if cand and Path(cand).is_dir():
                    return Path(cand)
        except Exception as e:  # noqa: BLE001
            log.debug("sandbox public resolve: %s", e)
    for cand in (
        AO_ROOT / "public",
        Path("/root/array-operator/public"),
    ):
        if cand.is_dir():
            return cand
    return None


def _stage_public(src: Path) -> Path:
    """Copy public tree to a clean temp dir (no netlify.toml)."""
    import shutil

    td = Path(tempfile.mkdtemp(prefix="ao-chamber-"))
    dest = td / "public"
    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns(
            "netlify.toml", "edge-functions", ".git", "__pycache__", "*.pyc"
        ),
    )
    # Hard refuse if anything slipped through
    if (dest / "netlify.toml").exists():
        (dest / "netlify.toml").unlink()
    return dest


def deploy_chamber(
    *,
    public_dir: str | Path | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    title: str | None = None,
    db=None,
) -> dict[str, Any]:
    """Deploy AO public/ as chamber branch deploy. Never touches prod publish."""
    if not chamber_enabled():
        return {"ok": False, "skipped": True, "reason": "SOVEREIGN_CHAMBER_DEPLOY off"}

    src = resolve_public_dir(run_id=run_id, public_dir=public_dir)
    if src is None:
        return {"ok": False, "error": "no public/ dir found for chamber"}

    script = _find_deploy_script()
    if script is None:
        return {
            "ok": False,
            "error": "chamber_deploy_dir.py not found",
            "hint": "expected array-operator/scripts/chamber_deploy_dir.py",
        }

    staged = _stage_public(src)
    out_json = staged.parent / "chamber_result.json"
    env = os.environ.copy()
    env["CHAMBER_URL_OUT"] = str(out_json)
    # Ensure token available in worker if only hermes secret exists
    if not env.get("NETLIFY_AUTH_TOKEN") and not env.get("NETLIFY_TOKEN"):
        tok_path = Path.home() / ".hermes" / "secrets" / "netlify_token"
        if tok_path.is_file():
            env["NETLIFY_AUTH_TOKEN"] = tok_path.read_text(encoding="utf-8").strip()

    try:
        r = subprocess.run(
            ["python3", str(script), str(staged)],
            capture_output=True,
            text=True,
            timeout=int(os.getenv("SOVEREIGN_CHAMBER_DEPLOY_TIMEOUT", "600")),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "chamber deploy timeout"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:400]}

    result: dict[str, Any] = {
        "ok": r.returncode == 0,
        "returncode": r.returncode,
        "public_src": str(src),
        "run_id": run_id,
        "job_id": job_id,
        "title": title,
        "at": _utcnow_iso(),
    }
    if out_json.is_file():
        try:
            result.update(json.loads(out_json.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            result["parse_warn"] = str(e)[:200]
    if r.returncode != 0:
        result["ok"] = False
        result["stderr"] = (r.stderr or "")[-1500:]
        result["stdout"] = (r.stdout or "")[-1500:]
        log.warning("chamber deploy failed: %s", result.get("stderr") or result)
        return result

    url = (result.get("chamber_url") or default_chamber_url()).rstrip("/")
    result["chamber_url"] = url
    result["ok"] = True

    # Persist on sandbox run + durable memory
    if run_id:
        try:
            from .energy_agent_sovereign_mind_sandbox import load_run, save_run

            run = load_run(run_id)
            if run:
                hist = list(run.get("chamber_deploys") or [])
                hist.append(
                    {
                        "at": result["at"],
                        "url": url,
                        "deploy_id": result.get("deploy_id"),
                        "job_id": job_id,
                        "title": title,
                        "files": result.get("files"),
                    }
                )
                run["chamber_deploys"] = hist[-50:]
                run["chamber_url"] = url
                run["chamber_deploy_id"] = result.get("deploy_id")
                run["chamber_deployed_at"] = result["at"]
                save_run(run)
        except Exception as e:  # noqa: BLE001
            log.warning("chamber run meta: %s", e)

    if db is not None:
        try:
            from .energy_agent_sovereign import memory_set, write_note

            memory_set(
                db,
                CHAMBER_META_KEY,
                json.dumps(
                    {
                        "url": url,
                        "deploy_id": result.get("deploy_id"),
                        "at": result["at"],
                        "run_id": run_id,
                        "job_id": job_id,
                    }
                ),
                source="sovereign_chamber",
            )
            write_note(
                db,
                kind="memory",
                title=f"Chamber ship {result.get('deploy_id') or ''}".strip(),
                body=(
                    f"Chamber URL: {url}\n"
                    f"Source public/: {src}\n"
                    f"Run: {run_id or '—'}\n"
                    f"Job: {job_id or '—'}\n"
                    f"Title: {title or '—'}\n"
                ),
                provider="sovereign_chamber",
            )
        except Exception as e:  # noqa: BLE001
            log.debug("chamber memory: %s", e)

    try:
        from .energy_agent_sovereign_reality import record_ship

        record_ship(
            title=f"[chamber] {title or 'deploy'}",
            repo="array-operator",
            job_id=job_id,
            files=[],
            sha=result.get("deploy_id"),
            brief=f"Chamber URL {url}",
            source="sovereign_chamber",
        )
    except Exception as e:  # noqa: BLE001
        log.debug("chamber reality: %s", e)

    log.info("chamber ready: %s", url)
    return result


def get_chamber_status(db=None) -> dict[str, Any]:
    """Status for portal / admin / drive URL resolution."""
    url = default_chamber_url()
    meta: dict[str, Any] = {}
    run_url = None
    run_id = None
    try:
        from .energy_agent_sovereign_mind_sandbox import get_active_run

        active = get_active_run(db)
        if active:
            run_id = active.get("id")
            run_url = active.get("chamber_url")
            meta["run"] = {
                "id": run_id,
                "chamber_url": run_url,
                "chamber_deployed_at": active.get("chamber_deployed_at"),
                "deploys": len(active.get("chamber_deploys") or []),
            }
    except Exception as e:  # noqa: BLE001
        meta["run_err"] = str(e)[:200]

    if db is not None:
        try:
            from .energy_agent_sovereign import memory_get_all

            for m in memory_get_all(db, limit=40):
                if m.get("key") == CHAMBER_META_KEY and m.get("value"):
                    try:
                        meta["memory"] = json.loads(m["value"])
                    except json.JSONDecodeError:
                        meta["memory"] = {"raw": m["value"][:200]}
                    break
        except Exception as e:  # noqa: BLE001
            meta["memory_err"] = str(e)[:200]

    # Prefer last successful deploy URL over env default
    for candidate in (
        run_url,
        (meta.get("memory") or {}).get("url") if isinstance(meta.get("memory"), dict) else None,
        url,
    ):
        if candidate:
            url = str(candidate).rstrip("/")
            break

    return {
        "ok": True,
        "chamber_url": url,
        "default_url": DEFAULT_CHAMBER_URL,
        "enabled": chamber_enabled(),
        "mode": "netlify_branch_deploy",
        "branch": os.getenv("NETLIFY_CHAMBER_BRANCH") or "chamber",
        "site_id": os.getenv("NETLIFY_CHAMBER_SITE_ID")
        or "966cb1f5-944e-41fd-855b-10053edc5d18",
        "prod_untouched": True,
        "level": "L2",
        "meta": meta,
        "run_id": run_id,
    }


def resolve_chamber_url(db=None) -> str:
    """URL the mind should treat as the product it owns."""
    try:
        st = get_chamber_status(db)
        if st.get("chamber_url"):
            return str(st["chamber_url"]).rstrip("/")
    except Exception:
        pass
    return default_chamber_url()
