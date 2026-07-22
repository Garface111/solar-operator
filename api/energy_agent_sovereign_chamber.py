"""Sovereign Chamber L2 — always-on false-real Array Operator URL.

Deploys sandbox (or baseline) AO ``public/`` as a Netlify **branch deploy**
on the existing production site. Never publishes arrayoperator.com.

Stable URL (default branch name ``chamber``):
  https://chamber--array-operator-ea.netlify.app

Deploy is **self-contained** (inline Netlify REST) so Railway workers do not
need array-operator/scripts on disk. Optional external script still preferred
when present (local laptop).

See docs/sovereign/ROCKET_ENGINE.md (L2 / L4).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
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
DEFAULT_PROD_URL = "https://arrayoperator.com"
PROD_SITE_ID = "966cb1f5-944e-41fd-855b-10053edc5d18"
CHAMBER_META_KEY = "sovereign_chamber"
NETLIFY_API = "https://api.netlify.com/api/v1"


def chamber_enabled() -> bool:
    return (os.getenv("SOVEREIGN_CHAMBER_DEPLOY", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def default_chamber_url() -> str:
    return (
        os.getenv("SOVEREIGN_CHAMBER_URL")
        or DEFAULT_CHAMBER_URL
    ).rstrip("/")


def default_prod_url() -> str:
    return (os.getenv("AO_APP_URL") or DEFAULT_PROD_URL).rstrip("/")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _netlify_token() -> str | None:
    t = (os.getenv("NETLIFY_AUTH_TOKEN") or os.getenv("NETLIFY_TOKEN") or "").strip()
    if t:
        return t
    p = Path.home() / ".hermes" / "secrets" / "netlify_token"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return None


def _find_deploy_script() -> Path | None:
    candidates = [
        Path(os.getenv("CHAMBER_DEPLOY_SCRIPT") or ""),
        SO_ROOT / "scripts" / "chamber_deploy_dir.py",
        AO_ROOT / "scripts" / "chamber_deploy_dir.py",
        Path("/root/array-operator/scripts/chamber_deploy_dir.py"),
        SO_ROOT.parent / "array-operator" / "scripts" / "chamber_deploy_dir.py",
    ]
    for p in candidates:
        if p and p.is_file():
            return p
    return None


def _http_req(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, bytes]:
    hdrs = dict(headers or {})
    r = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def _walk_public_files(root: Path) -> dict[str, tuple[str, bytes]]:
    out: dict[str, tuple[str, bytes]] = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            full = Path(dirpath) / n
            rel = "/" + str(full.relative_to(root)).replace(os.sep, "/")
            data = full.read_bytes()
            out[rel] = (hashlib.sha1(data).hexdigest(), data)
    return out


def _inline_netlify_branch_deploy(public_dir: Path) -> dict[str, Any]:
    """Deploy public/ as Netlify draft+branch (never production context)."""
    token = _netlify_token()
    if not token:
        return {"ok": False, "error": "no NETLIFY_AUTH_TOKEN / netlify_token"}

    site = os.getenv("NETLIFY_CHAMBER_SITE_ID") or PROD_SITE_ID
    branch = (os.getenv("NETLIFY_CHAMBER_BRANCH") or "chamber").strip() or "chamber"

    if (public_dir / "netlify.toml").exists():
        return {"ok": False, "error": "netlify.toml in deploy dir — refuse"}
    if (public_dir / "netlify").is_dir() or (public_dir / "edge-functions").is_dir():
        return {"ok": False, "error": "edge-functions/netlify dir present — refuse"}

    files = _walk_public_files(public_dir)
    banned = [
        p
        for p in files
        if "edge-functions" in p or p.endswith("/gate.ts") or p.endswith("netlify.toml")
    ]
    if banned:
        return {"ok": False, "error": f"banned paths: {banned[:3]}"}
    if not files:
        return {"ok": False, "error": "no files"}

    auth = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "sovereign-chamber/1.0",
        "Content-Type": "application/json",
    }
    digest = {p: sha for p, (sha, _) in files.items()}
    payload = {
        "files": digest,
        "draft": True,
        "branch": branch,
        "title": f"sovereign-chamber {branch}",
    }
    st, body = _http_req(
        "POST",
        f"{NETLIFY_API}/sites/{site}/deploys",
        body=json.dumps(payload).encode(),
        headers=auth,
    )
    if st not in (200, 201):
        return {"ok": False, "error": f"create deploy {st}: {body[:300]!r}"}
    dep = json.loads(body)
    if dep.get("context") == "production":
        return {"ok": False, "error": "FATAL: deploy context production — aborted"}
    dep_id = dep["id"]
    required = set(dep.get("required") or [])
    uploaded = 0
    for path, (sha, data) in files.items():
        if sha not in required:
            continue
        st, b = _http_req(
            "PUT",
            f"{NETLIFY_API}/deploys/{dep_id}/files{path}",
            body=data,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "sovereign-chamber/1.0",
                "Content-Type": "application/octet-stream",
            },
        )
        if st not in (200, 201):
            return {"ok": False, "error": f"upload {path} {st}: {b[:200]!r}"}
        uploaded += 1

    final = dep
    for _ in range(60):
        st, b = _http_req(
            "GET",
            f"{NETLIFY_API}/deploys/{dep_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "sovereign-chamber/1.0",
            },
        )
        if st != 200:
            time.sleep(2)
            continue
        final = json.loads(b)
        if final.get("state") == "ready":
            break
        if final.get("state") == "error":
            return {"ok": False, "error": f"deploy error: {b[:300]!r}"}
        time.sleep(2)

    if final.get("context") == "production":
        return {"ok": False, "error": "FATAL: finished as production"}
    if final.get("edge_functions_present") is True:
        return {"ok": False, "error": "edge_functions_present on chamber deploy"}

    url = (
        final.get("deploy_ssl_url")
        or final.get("deploy_url")
        or f"https://{branch}--array-operator-ea.netlify.app"
    ).replace("http://", "https://").rstrip("/")
    if url in (DEFAULT_PROD_URL, "https://arrayoperator.com"):
        url = f"https://{branch}--array-operator-ea.netlify.app"

    return {
        "ok": True,
        "deploy_id": dep_id,
        "context": final.get("context"),
        "branch": final.get("branch") or branch,
        "state": final.get("state"),
        "chamber_url": url,
        "files": len(files),
        "uploaded": uploaded,
        "site_id": site,
        "provider": "inline_netlify",
        "note": "branch/draft deploy — production arrayoperator.com untouched",
    }


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
        Path("/tmp/sovereign-repos/array-operator/public"),
    ):
        if cand.is_dir():
            return cand
    return None


def resolve_baseline_public() -> Path | None:
    """Full prod-shaped AO public/ used as chamber foundation (never a thin landing)."""
    for cand in (
        Path(os.getenv("SOVEREIGN_CHAMBER_BASELINE_PUBLIC") or ""),
        AO_ROOT / "public",
        Path("/root/array-operator/public"),
        Path("/tmp/sovereign-repos/array-operator/public"),
    ):
        if cand and str(cand) not in (".", "") and cand.is_dir():
            idx = cand / "index.html"
            if idx.is_file() and _looks_like_ao_spa(idx.read_text(encoding="utf-8", errors="replace")):
                return cand
    return resolve_public_dir()  # last resort


def _looks_like_ao_spa(html: str) -> bool:
    """True when index is the real Array Operator owner SPA, not a marketing stub."""
    if not html or len(html) < 20_000:
        return False
    h = html.lower()
    # Real app shell markers (all should be present on prod index)
    need = ("so_session", "app.js", "array operator")
    if not all(n in h for n in need):
        return False
    # Thin hero landings Sovereign keeps inventing
    if "solar fleet command" in h and "so_session" not in h:
        return False
    if h.count("<script") < 3 and "hero-logo" in h:
        return False
    return True


REQUIRED_CHAMBER_FILES = (
    "index.html",
    "login.html",
    "app.js",
    "styles.css",
    "session-tabscope.js",
    "_redirects",
)


def validate_chamber_tree(root: Path) -> dict[str, Any]:
    """Ensure chamber tree is a full functional AO frontend twin."""
    missing = [f for f in REQUIRED_CHAMBER_FILES if not (root / f).is_file()]
    idx = root / "index.html"
    index_html = ""
    if idx.is_file():
        index_html = idx.read_text(encoding="utf-8", errors="replace")
    spa_ok = _looks_like_ao_spa(index_html)
    redirects = (root / "_redirects").read_text(encoding="utf-8", errors="replace") if (root / "_redirects").is_file() else ""
    has_v1_proxy = "/v1/*" in redirects and "railway.app" in redirects
    n_files = sum(1 for _p, _d, fs in os.walk(root) for _ in fs)
    ok = not missing and spa_ok and has_v1_proxy and n_files >= 80
    return {
        "ok": ok,
        "missing": missing,
        "spa_ok": spa_ok,
        "index_bytes": len(index_html.encode("utf-8")),
        "has_v1_proxy": has_v1_proxy,
        "file_count": n_files,
        "errors": (
            (["missing:" + ",".join(missing)] if missing else [])
            + (["index_not_spa"] if not spa_ok else [])
            + (["no_v1_proxy_redirects"] if not has_v1_proxy else [])
            + ([f"too_few_files:{n_files}"] if n_files < 80 else [])
        ),
    }


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


def build_full_chamber_tree(
    *,
    run_id: str | None = None,
    public_dir: str | Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Build a **full functional twin** of arrayoperator.com frontend.

    1. Start from baseline AO public/ (complete prod-shaped tree + _redirects)
    2. Overlay sandbox public/ changes on top (Sovereign's thrash)
    3. If sandbox replaced index.html with a thin marketing page, keep SPA as
       index.html and save the marketing page as /welcome.html instead
    4. Validate required files + SPA markers before deploy
    """
    import shutil

    meta: dict[str, Any] = {"overlay": False, "rescued_index": False}
    baseline = resolve_baseline_public()
    overlay = None
    if public_dir:
        op = Path(public_dir).expanduser().resolve()
        if op.is_dir():
            overlay = op
    if overlay is None and run_id:
        overlay = resolve_public_dir(run_id=run_id)
        # Don't double-use baseline as overlay
        if overlay and baseline and overlay.resolve() == baseline.resolve():
            overlay = None
    elif overlay is None:
        # Explicit public_dir none — still try sandbox active
        try:
            from .energy_agent_sovereign_mind_sandbox import get_active_run

            active = get_active_run(None)
            if active:
                overlay = resolve_public_dir(run_id=active.get("id"))
                if overlay and baseline and overlay.resolve() == baseline.resolve():
                    overlay = None
        except Exception:
            pass

    if baseline is None and overlay is None:
        return None, {"ok": False, "error": "no baseline or overlay public/"}

    td = Path(tempfile.mkdtemp(prefix="ao-chamber-full-"))
    dest = td / "public"
    dest.mkdir(parents=True)

    # Foundation: full baseline
    foundation = baseline or overlay
    assert foundation is not None
    shutil.copytree(
        foundation,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            "netlify.toml", "edge-functions", ".git", "__pycache__", "*.pyc"
        ),
    )
    meta["baseline"] = str(foundation)

    # Overlay sandbox deltas (preserve full twin)
    if overlay and baseline and overlay.resolve() != baseline.resolve():
        meta["overlay"] = True
        meta["overlay_src"] = str(overlay)
        for dirpath, _dirs, names in os.walk(overlay):
            for n in names:
                if n in ("netlify.toml",) or n.endswith(".pyc"):
                    continue
                full = Path(dirpath) / n
                rel = full.relative_to(overlay)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                # Special-case index.html: never replace SPA with thin landing
                if rel.as_posix() == "index.html":
                    try:
                        new_html = full.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if _looks_like_ao_spa(new_html):
                        shutil.copy2(full, target)
                    else:
                        # Rescue: marketing experiment lives at /welcome
                        welcome = dest / "welcome.html"
                        shutil.copy2(full, welcome)
                        meta["rescued_index"] = True
                        meta["welcome_bytes"] = len(new_html)
                        log.warning(
                            "chamber: refused thin index.html overlay (%s bytes); "
                            "kept SPA index, wrote welcome.html",
                            len(new_html),
                        )
                    continue
                shutil.copy2(full, target)

    # Ensure _redirects from baseline if overlay wiped them
    if not (dest / "_redirects").is_file() and baseline and (baseline / "_redirects").is_file():
        shutil.copy2(baseline / "_redirects", dest / "_redirects")
    if not (dest / "_headers").is_file() and baseline and (baseline / "_headers").is_file():
        shutil.copy2(baseline / "_headers", dest / "_headers")

    # Final SPA rescue from baseline if index still broken
    idx = dest / "index.html"
    if not idx.is_file() or not _looks_like_ao_spa(idx.read_text(encoding="utf-8", errors="replace")):
        if baseline and (baseline / "index.html").is_file():
            if idx.is_file():
                shutil.copy2(idx, dest / "welcome.html")
                meta["rescued_index"] = True
            shutil.copy2(baseline / "index.html", idx)
            meta["restored_spa_index"] = True

    if (dest / "netlify.toml").exists():
        (dest / "netlify.toml").unlink()

    check = validate_chamber_tree(dest)
    meta["validate"] = check
    if not check.get("ok"):
        return dest, {"ok": False, "error": "chamber_tree_invalid", **meta}
    meta["ok"] = True
    return dest, meta


def deploy_chamber(
    *,
    public_dir: str | Path | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    title: str | None = None,
    db=None,
) -> dict[str, Any]:
    """Deploy full AO frontend twin as chamber branch. Never touches prod publish."""
    if not chamber_enabled():
        return {"ok": False, "skipped": True, "reason": "SOVEREIGN_CHAMBER_DEPLOY off"}

    staged, build_meta = build_full_chamber_tree(run_id=run_id, public_dir=public_dir)
    if staged is None or not build_meta.get("ok"):
        return {
            "ok": False,
            "error": (build_meta or {}).get("error") or "chamber build failed",
            "build": build_meta,
        }

    result: dict[str, Any] = {
        "public_src": build_meta.get("overlay_src") or build_meta.get("baseline"),
        "build": build_meta,
        "run_id": run_id,
        "job_id": job_id,
        "title": title,
        "at": _utcnow_iso(),
    }

    # Prefer external script when available (local host); else inline REST (Railway).
    script = _find_deploy_script()
    used = "inline"
    if script is not None and (os.getenv("SOVEREIGN_CHAMBER_INLINE", "0") or "0").strip() not in (
        "1", "true", "yes", "on",
    ):
        out_json = staged.parent / "chamber_result.json"
        env = os.environ.copy()
        env["CHAMBER_URL_OUT"] = str(out_json)
        if not env.get("NETLIFY_AUTH_TOKEN") and not env.get("NETLIFY_TOKEN"):
            tok = _netlify_token()
            if tok:
                env["NETLIFY_AUTH_TOKEN"] = tok
        try:
            r = subprocess.run(
                ["python3", str(script), str(staged)],
                capture_output=True,
                text=True,
                timeout=int(os.getenv("SOVEREIGN_CHAMBER_DEPLOY_TIMEOUT", "600")),
                env=env,
            )
            used = "script"
            result["returncode"] = r.returncode
            if out_json.is_file():
                try:
                    result.update(json.loads(out_json.read_text(encoding="utf-8")))
                except Exception as e:  # noqa: BLE001
                    result["parse_warn"] = str(e)[:200]
            if r.returncode != 0:
                result["ok"] = False
                result["stderr"] = (r.stderr or "")[-1500:]
                result["stdout"] = (r.stdout or "")[-1500:]
                # Fall through to inline if script failed
                log.warning("chamber script failed, trying inline: %s", result.get("stderr"))
                used = "inline_fallback"
            else:
                result["ok"] = True
        except subprocess.TimeoutExpired:
            result["ok"] = False
            result["error"] = "chamber deploy timeout"
            used = "inline_fallback"
        except Exception as e:  # noqa: BLE001
            result["ok"] = False
            result["error"] = str(e)[:400]
            used = "inline_fallback"

    if used != "script" or not result.get("ok"):
        inline = _inline_netlify_branch_deploy(staged)
        result.update(inline)
        result["provider"] = inline.get("provider") or "inline_netlify"
        if not inline.get("ok"):
            log.warning("chamber deploy failed: %s", inline)
            result["ok"] = False
            return result

    result["deploy_via"] = used if used == "script" else result.get("provider", "inline")
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
                    f"Source public/: {result.get('public_src')}\n"
                    f"Build: {json.dumps(build_meta, default=str)[:1500]}\n"
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
