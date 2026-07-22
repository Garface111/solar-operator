"""Sovereign Mind Sandbox — free-run arena vs Ford baseline.

Ford 2026-07-22: let Sovereign run freely for a week (or any window), then
compare what *it* built to what *Ford* shipped in the same window. Goal: is the
mind fundamentally better, or not?

Hard rules:
- Sandbox never merges to main / never deploys prod.
- Work lands under SOVEREIGN_MIND_SANDBOX_ROOT (default /root/sovereign-sandbox)
  and optional git branches `sov/sandbox/<run_id>/…`.
- Reality file still records sandbox ships with source=`sovereign_sandbox`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("energy_agent.sovereign.mind_sandbox")

_PKG = Path(__file__).resolve().parent
SO_ROOT = Path(os.getenv("SOVEREIGN_REPO_ROOT") or _PKG.parent).resolve()
AO_ROOT = Path(
    os.getenv("ARRAY_OPERATOR_ROOT")
    or os.getenv("SOVEREIGN_AO_ROOT")
    or (SO_ROOT.parent / "array-operator")
).resolve()

# Default /tmp so Railway worker can write; local host may override to /root/…
SANDBOX_ROOT = Path(
    os.getenv("SOVEREIGN_MIND_SANDBOX_ROOT")
    or os.getenv("SOVEREIGN_REPO_CACHE", "/tmp/sovereign-repos") + "/sandbox"
).resolve()
# Meta may live in sandbox root when repo docs/ is not writable (prod worker)
RUNS_META = Path(
    os.getenv("SOVEREIGN_MIND_SANDBOX_META")
    or str(SANDBOX_ROOT / "runs_meta")
).resolve()
ACTIVE_KEY = "mind_sandbox_active_run"
HISTORY_KEY = "mind_sandbox_run_history"

# Authors treated as Ford (human baseline) when scoring
FORD_AUTHOR_RES = [
    re.compile(r"ford", re.I),
    re.compile(r"garface", re.I),
    re.compile(r"genereaux", re.I),
]


def mind_sandbox_enabled() -> bool:
    return (os.getenv("SOVEREIGN_MIND_SANDBOX", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def mind_sandbox_force() -> bool:
    """When true, ALL code jobs are sandbox-only (no main / no prod deploy)."""
    return (os.getenv("SOVEREIGN_MIND_SANDBOX_FORCE", "0") or "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def ensure_active_run(db=None, *, days: int = 7) -> dict[str, Any] | None:
    """Return open run; auto-start one when FORCE mode needs isolation."""
    active = get_active_run(db)
    if active and active.get("status") == "open":
        return active
    if not mind_sandbox_force() and not mind_sandbox_enabled():
        return None
    if not mind_sandbox_force():
        return None
    out = start_run(
        db,
        days=days,
        title="Auto free-run (sandbox-only mode)",
        goal="SOVEREIGN_MIND_SANDBOX_FORCE=1 — all code work stays off main/prod.",
        free_run=True,
    )
    return out.get("run") if out.get("ok") else load_run((out.get("run") or {}).get("id") or "")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utcnow()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def _id() -> str:
    return "sbox_" + uuid.uuid4().hex[:12]


def ensure_layout() -> None:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_META.mkdir(parents=True, exist_ok=True)
    readme = SO_ROOT / "docs" / "sovereign" / "mind_sandbox" / "README.md"
    if not readme.exists():
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text(
            "# Sovereign Mind Sandbox\n\n"
            "Free-run arena for Sovereign. No prod deploys.\n\n"
            "1. `POST /admin/sovereign/mind-sandbox/start` — open a run (default 7d)\n"
            "2. Sovereign code jobs with `sandbox: true` (or auto when a run is active "
            "and `SOVEREIGN_MIND_SANDBOX_FORCE=1`) land only in sandbox worktrees\n"
            "3. `POST /admin/sovereign/mind-sandbox/score` — compare Sovereign vs Ford\n"
            "4. End run → scorecard written under `runs/<id>/scorecard.json`\n",
            encoding="utf-8",
        )


def run_dir(run_id: str) -> Path:
    return SANDBOX_ROOT / run_id


def meta_path(run_id: str) -> Path:
    return RUNS_META / f"{run_id}.json"


def load_run(run_id: str) -> dict[str, Any] | None:
    p = meta_path(run_id)
    if not p.exists():
        # also check sandbox root
        alt = run_dir(run_id) / "run.json"
        p = alt if alt.exists() else p
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_run(run: dict[str, Any]) -> Path:
    ensure_layout()
    rid = run["id"]
    rd = run_dir(rid)
    rd.mkdir(parents=True, exist_ok=True)
    for p in (meta_path(rid), rd / "run.json"):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(run, indent=2, default=str) + "\n", encoding="utf-8")
    return meta_path(rid)


def get_active_run(db=None) -> dict[str, Any] | None:
    """Prefer durable memory; fall back to newest open run on disk."""
    if db is not None:
        try:
            from .energy_agent_sovereign import memory_get_all
            for m in memory_get_all(db, limit=40):
                if m.get("key") == ACTIVE_KEY and m.get("value"):
                    try:
                        data = json.loads(m["value"])
                        rid = data.get("id")
                        if rid:
                            full = load_run(rid)
                            if full and full.get("status") == "open":
                                return full
                    except json.JSONDecodeError:
                        pass
        except Exception as e:  # noqa: BLE001
            log.debug("active run memory read: %s", e)
    ensure_layout()
    best = None
    for p in sorted(RUNS_META.glob("sbox_*.json"), reverse=True):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if r.get("status") == "open":
            best = r
            break
    return best


def start_run(
    db=None,
    *,
    days: int = 7,
    title: str | None = None,
    goal: str | None = None,
    free_run: bool = True,
) -> dict[str, Any]:
    """Open a sandbox evaluation window."""
    ensure_layout()
    existing = get_active_run(db)
    if existing and existing.get("status") == "open":
        return {
            "ok": False,
            "error": "run_already_open",
            "run": existing,
            "hint": "End the active run before starting another, or use force_end.",
        }
    rid = _id()
    now = _utcnow()
    ends = now + timedelta(days=max(1, min(int(days), 30)))
    run = {
        "id": rid,
        "title": title or f"Mind sandbox {now.date().isoformat()}",
        "goal": goal
        or (
            "Sovereign free-runs product improvements in isolation. "
            "After the window, compare its ships to Ford's human ships."
        ),
        "status": "open",
        "free_run": bool(free_run),
        "started_at": _iso(now),
        "ends_at": _iso(ends),
        "days": int(days),
        "sovereign_jobs": [],
        "sovereign_commits": [],
        "ford_baseline_commits": [],
        "scorecard": None,
        "rules": {
            "no_prod_deploy": True,
            "no_main_merge": True,
            "branch_prefix": f"sov/sandbox/{rid}",
            "worktree": str(run_dir(rid)),
        },
    }
    save_run(run)
    # worktrees: clone-ish via git worktree if possible
    _prepare_worktrees(rid)
    if db is not None:
        try:
            from .energy_agent_sovereign import memory_set, write_note
            memory_set(
                db,
                ACTIVE_KEY,
                json.dumps({"id": rid, "ends_at": run["ends_at"], "title": run["title"]}),
                source="mind_sandbox",
            )
            write_note(
                db,
                kind="memory",
                title=f"Mind sandbox started {rid}",
                body=(
                    f"Free-run window open until {run['ends_at']}. "
                    f"Goal: {run['goal']}\nWorktree: {run_dir(rid)}"
                ),
                provider="mind_sandbox",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("sandbox start memory: %s", e)
    return {"ok": True, "run": run}


def _resolve_src_repo(name: str) -> Path | None:
    """Prefer worker cache / explicit roots, then monorepo neighbors."""
    cache = Path(os.getenv("SOVEREIGN_REPO_CACHE", "/tmp/sovereign-repos")).expanduser()
    candidates = []
    if name == "array-operator":
        candidates = [
            Path(os.getenv("SOVEREIGN_AO_REPO", "") or "").expanduser(),
            Path(os.getenv("ARRAY_OPERATOR_ROOT", "") or "").expanduser(),
            AO_ROOT,
            cache / "array-operator",
            Path("/root/array-operator"),
        ]
    else:
        candidates = [
            Path(os.getenv("SOVEREIGN_SO_REPO", "") or "").expanduser(),
            Path(os.getenv("SOVEREIGN_REPO_ROOT", "") or "").expanduser(),
            SO_ROOT,
            cache / "solar-operator",
            Path("/root/solar-operator"),
        ]
    for p in candidates:
        if not p or str(p) in (".", ""):
            continue
        try:
            pr = p.resolve()
        except Exception:
            continue
        if (pr / ".git").exists() or (pr / ".git").is_file() or (pr / "api").is_dir() or (pr / "public").is_dir():
            return pr
    return None


def _prepare_worktrees(run_id: str) -> dict[str, Any]:
    """Create sandbox workdirs (worktree or shallow copy) for AO + SO."""
    rd = run_dir(run_id)
    rd.mkdir(parents=True, exist_ok=True)
    out: dict[str, Any] = {}
    for name in ("array-operator", "solar-operator"):
        dest = rd / name
        if dest.exists():
            out[name] = {"ok": True, "path": str(dest), "note": "exists"}
            continue
        src = _resolve_src_repo(name)
        if src is None:
            out[name] = {"ok": False, "error": f"source missing for {name}"}
            continue
        # Prefer git worktree add
        try:
            branch = f"sov/sandbox/{run_id}/{name[:2]}"
            r = subprocess.run(
                ["git", "-C", str(src), "worktree", "add", "-b", branch, str(dest), "HEAD"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode == 0:
                out[name] = {
                    "ok": True,
                    "path": str(dest),
                    "branch": branch,
                    "method": "worktree",
                    "src": str(src),
                }
                continue
            # branch may exist — try without -b
            r2 = subprocess.run(
                ["git", "-C", str(src), "worktree", "add", str(dest), "HEAD"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r2.returncode == 0:
                out[name] = {
                    "ok": True,
                    "path": str(dest),
                    "method": "worktree_detached",
                    "src": str(src),
                }
                continue
            # Last resort: clone from local path (isolated, no link to main WT)
            r3 = subprocess.run(
                ["git", "clone", "--local", str(src), str(dest)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if r3.returncode == 0:
                out[name] = {
                    "ok": True,
                    "path": str(dest),
                    "method": "clone_local",
                    "src": str(src),
                }
                continue
            out[name] = {
                "ok": False,
                "error": (r.stderr or r2.stderr or r3.stderr or "worktree failed")[:400],
                "src": str(src),
            }
        except Exception as e:  # noqa: BLE001
            out[name] = {"ok": False, "error": str(e)[:400], "src": str(src)}
    (rd / "worktrees.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def register_sandbox_job(
    db,
    run_id: str,
    *,
    job_id: str,
    title: str,
    repo: str,
    result: dict | None = None,
) -> None:
    run = load_run(run_id)
    if not run:
        return
    jobs = list(run.get("sovereign_jobs") or [])
    jobs.append(
        {
            "job_id": job_id,
            "title": title,
            "repo": repo,
            "at": _iso(),
            "ok": (result or {}).get("ok"),
            "sha": (result or {}).get("sha"),
            "files": (result or {}).get("files") or [],
        }
    )
    run["sovereign_jobs"] = jobs[-200:]
    if result and result.get("sha"):
        commits = list(run.get("sovereign_commits") or [])
        commits.append(
            {
                "sha": result.get("sha"),
                "repo": repo,
                "summary": title,
                "at": _iso(),
                "files": result.get("files") or [],
                "job_id": job_id,
            }
        )
        run["sovereign_commits"] = commits[-300:]
    save_run(run)
    # reality trail
    try:
        from .energy_agent_sovereign_reality import record_ship
        record_ship(
            title=f"[sandbox {run_id}] {title}",
            repo=repo,
            job_id=job_id,
            files=result.get("files") if result else None,
            sha=result.get("sha") if result else None,
            source="sovereign_sandbox",
        )
    except Exception as e:  # noqa: BLE001
        log.debug("sandbox reality record: %s", e)


def end_run(db=None, *, run_id: str | None = None, score: bool = True) -> dict[str, Any]:
    run = load_run(run_id) if run_id else get_active_run(db)
    if not run:
        return {"ok": False, "error": "no_run"}
    run["status"] = "closed"
    run["ended_at"] = _iso()
    if score:
        card = build_scorecard(run)
        run["scorecard"] = card
        sc_path = run_dir(run["id"]) / "scorecard.json"
        sc_path.write_text(json.dumps(card, indent=2, default=str) + "\n", encoding="utf-8")
        md_path = run_dir(run["id"]) / "comparison.md"
        md_path.write_text(_scorecard_markdown(card, run), encoding="utf-8")
    save_run(run)
    if db is not None:
        try:
            from .energy_agent_sovereign import memory_set, write_note
            memory_set(db, ACTIVE_KEY, "", source="mind_sandbox")
            # append history
            hist = []
            try:
                from .energy_agent_sovereign import memory_get_all
                for m in memory_get_all(db, limit=40):
                    if m.get("key") == HISTORY_KEY and m.get("value"):
                        hist = json.loads(m["value"])
                        break
            except Exception:
                hist = []
            if not isinstance(hist, list):
                hist = []
            hist.append(
                {
                    "id": run["id"],
                    "ended_at": run.get("ended_at"),
                    "verdict": (run.get("scorecard") or {}).get("verdict"),
                }
            )
            memory_set(
                db, HISTORY_KEY, json.dumps(hist[-30:], default=str), source="mind_sandbox"
            )
            write_note(
                db,
                kind="decision",
                title=f"Mind sandbox closed {run['id']}",
                body=_scorecard_markdown(run.get("scorecard") or {}, run)[:8000],
                provider="mind_sandbox",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("end_run memory: %s", e)
    return {"ok": True, "run": run}


def _is_ford_author(author: str) -> bool:
    return any(rx.search(author or "") for rx in FORD_AUTHOR_RES)


def _is_sovereign_author(author: str, subject: str = "") -> bool:
    a = (author or "").lower()
    s = (subject or "").lower()
    return "sovereign" in a or s.startswith("sovereign:") or "sov/" in s


def _git_commits_between(
    repo: Path, *, since: str, until: str | None = None, max_count: int = 200
) -> list[dict]:
    if not repo.exists():
        return []
    cmd = [
        "git", "-C", str(repo), "log",
        f"--since={since}",
        f"--max-count={max_count}",
        "--date=iso-strict",
        "--pretty=format:%H%x09%ad%x09%an%x09%s",
        "--name-only",
    ]
    if until:
        cmd.insert(4, f"--until={until}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    commits: list[dict] = []
    cur = None
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        if "\t" in line and re.match(r"^[0-9a-f]{7,40}\t", line):
            if cur:
                commits.append(cur)
            p = line.split("\t", 3)
            cur = {
                "sha": p[0],
                "ts": p[1] if len(p) > 1 else None,
                "author": p[2] if len(p) > 2 else "",
                "summary": p[3] if len(p) > 3 else "",
                "files": [],
            }
        elif cur is not None:
            cur["files"].append(line.strip())
    if cur:
        commits.append(cur)
    return commits


def build_scorecard(run: dict[str, Any]) -> dict[str, Any]:
    """Compare sovereign sandbox activity vs Ford commits on main in the window."""
    since = run.get("started_at") or _iso()
    until = run.get("ended_at") or _iso()
    ford: list[dict] = []
    human_other: list[dict] = []
    sov_main: list[dict] = []  # accidental main ships by sovereign during window
    for name, root in (("array-operator", AO_ROOT), ("solar-operator", SO_ROOT)):
        for c in _git_commits_between(root, since=since, until=until):
            row = {**c, "repo": name}
            if _is_sovereign_author(c.get("author") or "", c.get("summary") or ""):
                sov_main.append(row)
            elif _is_ford_author(c.get("author") or ""):
                ford.append(row)
            else:
                human_other.append(row)

    sov_jobs = list(run.get("sovereign_jobs") or [])
    sov_commits = list(run.get("sovereign_commits") or [])

    def _files_touched(rows: list[dict]) -> set[str]:
        s: set[str] = set()
        for r in rows:
            for f in r.get("files") or []:
                s.add(f"{r.get('repo','?')}:{f}")
        return s

    ford_files = _files_touched(ford)
    sov_files = _files_touched(sov_commits)
    overlap = ford_files & sov_files

    # Simple heuristic scores (0–100)
    volume_sov = len(sov_jobs) + len(sov_commits)
    volume_ford = len(ford)
    breadth_sov = len(sov_files)
    breadth_ford = len(ford_files)

    def clamp(x: float) -> int:
        return max(0, min(100, int(round(x))))

    scores = {
        "volume": clamp(50 + 10 * (volume_sov - volume_ford)),
        "breadth": clamp(50 + 5 * (breadth_sov - breadth_ford)),
        "overlap_with_ford": clamp(100 - 15 * len(overlap)) if (ford or sov_commits) else 50,
        # sandbox purity: penalize if it also merged main
        "sandbox_purity": 100 if not sov_main else clamp(100 - 20 * len(sov_main)),
    }
    overall = clamp(sum(scores.values()) / max(1, len(scores)))

    if volume_sov == 0 and volume_ford == 0:
        verdict = "inconclusive_no_activity"
        narrative = "Neither Sovereign nor Ford shipped in this window."
    elif volume_sov == 0:
        verdict = "ford_ahead_sovereign_idle"
        narrative = "Ford shipped; Sovereign sandbox produced nothing."
    elif volume_ford == 0 and volume_sov > 0:
        verdict = "sovereign_only"
        narrative = (
            "Sovereign produced sandbox work; Ford had no commits in-window. "
            "Review quality manually before declaring victory."
        )
    elif overall >= 60 and volume_sov >= volume_ford:
        verdict = "sovereign_edge"
        narrative = (
            "Sovereign matched or beat Ford on volume/breadth in sandbox. "
            "Read comparison.md for substance — numbers alone aren't product taste."
        )
    elif overall <= 40:
        verdict = "ford_edge"
        narrative = "Ford's human ships look stronger on the automatic score. Study the gap."
    else:
        verdict = "mixed"
        narrative = "Close race. Use the file lists and job titles to judge product quality."

    return {
        "generated_at": _iso(),
        "window": {"since": since, "until": until},
        "counts": {
            "sovereign_jobs": len(sov_jobs),
            "sovereign_sandbox_commits": len(sov_commits),
            "sovereign_main_commits": len(sov_main),
            "ford_commits": len(ford),
            "other_human_commits": len(human_other),
            "sovereign_files": breadth_sov,
            "ford_files": breadth_ford,
            "file_overlap": len(overlap),
        },
        "scores": scores,
        "overall": overall,
        "verdict": verdict,
        "narrative": narrative,
        "ford_commits": [
            {"sha": c["sha"][:12], "repo": c["repo"], "summary": c["summary"], "author": c["author"]}
            for c in ford[:40]
        ],
        "sovereign_sandbox": [
            {
                "sha": (c.get("sha") or "")[:12],
                "repo": c.get("repo"),
                "summary": c.get("summary"),
                "job_id": c.get("job_id"),
            }
            for c in sov_commits[:40]
        ],
        "sovereign_jobs": [
            {"job_id": j.get("job_id"), "title": j.get("title"), "repo": j.get("repo"), "ok": j.get("ok")}
            for j in sov_jobs[:40]
        ],
        "overlap_files": sorted(overlap)[:40],
    }


def _scorecard_markdown(card: dict, run: dict) -> str:
    if not card:
        return f"# Sandbox {run.get('id')}\n\nNo scorecard.\n"
    c = card.get("counts") or {}
    s = card.get("scores") or {}
    lines = [
        f"# Mind sandbox comparison — {run.get('id')}",
        "",
        f"**Title:** {run.get('title')}",
        f"**Window:** {card.get('window',{}).get('since')} → {card.get('window',{}).get('until')}",
        f"**Verdict:** `{card.get('verdict')}` (overall {card.get('overall')}/100)",
        "",
        card.get("narrative") or "",
        "",
        "## Counts",
        f"- Sovereign jobs: {c.get('sovereign_jobs')}",
        f"- Sovereign sandbox commits: {c.get('sovereign_sandbox_commits')}",
        f"- Sovereign main commits (should be 0): {c.get('sovereign_main_commits')}",
        f"- Ford commits: {c.get('ford_commits')}",
        f"- File overlap: {c.get('file_overlap')}",
        "",
        "## Scores",
    ]
    for k, v in s.items():
        lines.append(f"- {k}: {v}")
    lines += ["", "## Ford ships"]
    for x in card.get("ford_commits") or []:
        lines.append(f"- `{x.get('sha')}` [{x.get('repo')}] {x.get('summary')}")
    lines += ["", "## Sovereign sandbox"]
    for x in card.get("sovereign_sandbox") or []:
        lines.append(f"- `{x.get('sha')}` [{x.get('repo')}] {x.get('summary')}")
    for j in card.get("sovereign_jobs") or []:
        lines.append(f"- job `{j.get('job_id')}` {j.get('title')} ok={j.get('ok')}")
    lines.append("")
    return "\n".join(lines)


def score_active(db=None, *, run_id: str | None = None) -> dict[str, Any]:
    run = load_run(run_id) if run_id else get_active_run(db)
    if not run:
        return {"ok": False, "error": "no_run"}
    # refresh ford baseline live
    card = build_scorecard(run)
    run["scorecard"] = card
    run["ford_baseline_commits"] = card.get("ford_commits") or []
    save_run(run)
    sc_path = run_dir(run["id"]) / "scorecard.json"
    sc_path.write_text(json.dumps(card, indent=2, default=str) + "\n", encoding="utf-8")
    (run_dir(run["id"]) / "comparison.md").write_text(
        _scorecard_markdown(card, run), encoding="utf-8"
    )
    return {"ok": True, "run_id": run["id"], "scorecard": card}


def sandbox_repo_path(run_id: str, repo: str) -> Path | None:
    p = run_dir(run_id) / repo
    return p if p.exists() else None


def status(db=None) -> dict[str, Any]:
    ensure_layout()
    active = get_active_run(db)
    runs = sorted(RUNS_META.glob("sbox_*.json"))
    return {
        "enabled": mind_sandbox_enabled(),
        "root": str(SANDBOX_ROOT),
        "active": active,
        "run_count_meta": len(runs),
        "force_all_jobs": (os.getenv("SOVEREIGN_MIND_SANDBOX_FORCE", "0") or "0")
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
    }


def wake_payload(db=None) -> dict[str, Any]:
    """Inject into cortex so it knows when free-run is active."""
    st = status(db)
    active = st.get("active")
    if not active:
        return {
            "active": False,
            "doctrine": (
                "Mind sandbox is idle. You may propose starting a free-run evaluation "
                "via admin/mind-sandbox or memory, but do not claim a sandbox is open."
            ),
        }
    return {
        "active": True,
        "run_id": active.get("id"),
        "title": active.get("title"),
        "ends_at": active.get("ends_at"),
        "goal": active.get("goal"),
        "jobs_so_far": len(active.get("sovereign_jobs") or []),
        "doctrine": (
            "MIND SANDBOX is OPEN. Free-run: prefer sandbox jobs "
            "(sandbox:true / kind sandbox_job). Do NOT merge main or deploy prod "
            "for experimental work. Compete with Ford's real ships this week — "
            "substance over thrash. After ends_at, expect a scorecard."
        ),
        "worktree": str(run_dir(active["id"])),
    }
