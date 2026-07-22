"""L4 — weekly prod vs chamber scorecard (outside the rocket room).

Ford scores whether the chamber is better than production. This module
collects cold evidence; taste is a human vote (or portal button).

Axes (ROCKET_ENGINE.md):
  delta, taste, safety, honesty, novelty, health
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("energy_agent.sovereign.chamber_score")

SCORECARD_KEY = "sovereign_chamber_scorecard"
TASTE_KEY = "sovereign_chamber_taste"
SCORE_HISTORY_KEY = "sovereign_chamber_score_history"

_PKG = Path(__file__).resolve().parent
SO_ROOT = Path(os.getenv("SOVEREIGN_REPO_ROOT") or _PKG.parent).resolve()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    d = dt or _utcnow()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def _probe_url(url: str, timeout: int = 20) -> dict[str, Any]:
    """HTTP probe: status, bytes, sha1 of body, title snippet, gate detection."""
    out: dict[str, Any] = {
        "url": url,
        "ok": False,
        "status": None,
        "bytes": 0,
        "sha1": None,
        "title": None,
        "has_private_preview_gate": False,
        "error": None,
    }
    try:
        req = urllib.request.Request(
            url if url.endswith("/") else url + "/",
            headers={"User-Agent": "sovereign-chamber-score/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            out["status"] = resp.status
            out["ok"] = 200 <= resp.status < 400
            out["bytes"] = len(body)
            out["sha1"] = hashlib.sha1(body).hexdigest()
            # title
            m = re.search(rb"<title[^>]*>([^<]{1,120})</title>", body, re.I)
            if m:
                out["title"] = m.group(1).decode("utf-8", errors="replace").strip()
            text = body.decode("utf-8", errors="replace")
            out["has_private_preview_gate"] = bool(
                re.search(r"Private preview|preprod environment|Request access", text, re.I)
            )
            # robots / chamber tell (headers hard to get via urlopen simply)
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["error"] = f"HTTP {e.code}"
        try:
            body = e.read()
            out["bytes"] = len(body)
            out["sha1"] = hashlib.sha1(body).hexdigest()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:300]
    return out


def _clamp(x: float) -> int:
    return max(0, min(100, int(round(x))))


def _read_taste(db=None) -> dict[str, Any]:
    if db is None:
        return {}
    try:
        from .energy_agent_sovereign import memory_get_all

        for m in memory_get_all(db, limit=50):
            if m.get("key") == TASTE_KEY and m.get("value"):
                try:
                    return json.loads(m["value"])
                except json.JSONDecodeError:
                    return {"raw": m["value"][:200]}
    except Exception as e:  # noqa: BLE001
        log.debug("taste read: %s", e)
    return {}


def set_taste_vote(
    db,
    *,
    preference: str,
    note: str | None = None,
    voter: str = "Ford",
) -> dict[str, Any]:
    """Record Ford's taste: chamber | prod | tie | abstain."""
    pref = (preference or "").strip().lower()
    if pref not in ("chamber", "prod", "production", "tie", "abstain"):
        return {"ok": False, "error": "preference must be chamber|prod|tie|abstain"}
    if pref == "production":
        pref = "prod"
    vote = {
        "preference": pref,
        "note": (note or "")[:2000],
        "voter": (voter or "Ford")[:120],
        "at": _iso(),
    }
    try:
        from .energy_agent_sovereign import memory_set, write_note

        memory_set(db, TASTE_KEY, json.dumps(vote), source="chamber_score")
        write_note(
            db,
            kind="memory",
            title=f"Chamber taste: {pref}",
            body=f"{vote['voter']} prefers **{pref}**.\n{vote.get('note') or ''}",
            provider="chamber_score",
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "vote": vote}


def build_chamber_scorecard(db=None, *, days: int = 7) -> dict[str, Any]:
    """Full L4 scorecard: health + delta + safety + honesty + taste + novelty."""
    from .energy_agent_sovereign_chamber import (
        default_chamber_url,
        default_prod_url,
        get_chamber_status,
    )

    days = max(1, min(int(days or 7), 30))
    until = _utcnow()
    since = until - timedelta(days=days)

    chamber_url = default_chamber_url()
    prod_url = default_prod_url()
    st = get_chamber_status(db)
    if st.get("chamber_url"):
        chamber_url = str(st["chamber_url"]).rstrip("/")

    # ── Health probes ──────────────────────────────────────────────────
    chamber_probe = _probe_url(chamber_url)
    prod_probe = _probe_url(prod_url)
    health = {
        "chamber": chamber_probe,
        "prod": prod_probe,
        "chamber_up": bool(chamber_probe.get("ok")),
        "prod_up": bool(prod_probe.get("ok")),
        "prod_gate_clean": not bool(prod_probe.get("has_private_preview_gate")),
        "surfaces_differ": (
            chamber_probe.get("sha1")
            and prod_probe.get("sha1")
            and chamber_probe.get("sha1") != prod_probe.get("sha1")
        ),
        "byte_delta": (chamber_probe.get("bytes") or 0) - (prod_probe.get("bytes") or 0),
    }

    # ── Sandbox / ship delta ───────────────────────────────────────────
    sandbox_card = None
    run_meta: dict[str, Any] = {}
    try:
        from .energy_agent_sovereign_mind_sandbox import (
            get_active_run,
            build_scorecard as build_sandbox_scorecard,
            load_run,
        )

        active = get_active_run(db)
        if active:
            run_meta = {
                "id": active.get("id"),
                "started_at": active.get("started_at"),
                "ends_at": active.get("ends_at"),
                "status": active.get("status"),
                "chamber_url": active.get("chamber_url"),
                "chamber_deploys": len(active.get("chamber_deploys") or []),
                "sovereign_jobs": len(active.get("sovereign_jobs") or []),
                "sovereign_commits": len(active.get("sovereign_commits") or []),
            }
            # Window-limit the scorecard if run is long
            sandbox_card = build_sandbox_scorecard(active)
    except Exception as e:  # noqa: BLE001
        run_meta["error"] = str(e)[:200]

    # Reality file ships in window
    reality_ships: list[dict] = []
    try:
        from .energy_agent_sovereign_reality import read_entries

        for e in read_entries(limit=200):
            at = e.get("at") or e.get("ts") or e.get("time") or ""
            if at and at < since.isoformat():
                continue
            src = (e.get("source") or "").lower()
            if "sandbox" in src or "chamber" in src or "sovereign" in src:
                reality_ships.append(
                    {
                        "at": at,
                        "title": e.get("title") or e.get("summary"),
                        "source": e.get("source"),
                        "repo": e.get("repo"),
                    }
                )
    except Exception as e:  # noqa: BLE001
        log.debug("reality read: %s", e)

    taste = _read_taste(db)

    # ── Scores 0–100 ───────────────────────────────────────────────────
    scores: dict[str, int] = {}
    # health: both up, prod clean, chamber reachable
    h = 0
    if health["chamber_up"]:
        h += 40
    if health["prod_up"]:
        h += 30
    if health["prod_gate_clean"]:
        h += 20
    if health["chamber_up"] and not chamber_probe.get("has_private_preview_gate"):
        h += 10
    scores["health"] = _clamp(h)

    # delta: sandbox activity + surface difference
    n_jobs = run_meta.get("sovereign_jobs") or 0
    n_commits = run_meta.get("sovereign_commits") or 0
    n_deploys = run_meta.get("chamber_deploys") or 0
    d = 20  # baseline if chamber exists
    d += min(40, 8 * (n_jobs + n_commits))
    d += min(20, 10 * n_deploys)
    if health.get("surfaces_differ"):
        d += 20
    elif n_commits or n_deploys:
        d += 5  # claimed work but index identical — weak signal
    scores["delta"] = _clamp(d)

    # safety: prod gate clean + sandbox purity
    purity = 100
    if sandbox_card:
        purity = int((sandbox_card.get("scores") or {}).get("sandbox_purity") or 100)
    s = purity * 0.7
    if health["prod_gate_clean"] and health["prod_up"]:
        s += 30
    scores["safety"] = _clamp(s)

    # honesty: reality ships vs sandbox jobs that claim success
    claimed_ok = 0
    if sandbox_card:
        for j in sandbox_card.get("sovereign_jobs") or []:
            if j.get("ok"):
                claimed_ok += 1
    recorded = len(reality_ships)
    if claimed_ok == 0 and recorded == 0:
        scores["honesty"] = 60  # nothing to lie about
    elif claimed_ok == 0:
        scores["honesty"] = 70
    else:
        ratio = recorded / max(1, claimed_ok)
        scores["honesty"] = _clamp(40 + 60 * min(1.0, ratio))

    # novelty: file overlap inverse from sandbox card + surface differ
    if sandbox_card and sandbox_card.get("scores"):
        scores["novelty"] = int(
            (sandbox_card["scores"].get("overlap_with_ford") or 50)
        )
    else:
        scores["novelty"] = 50 if health.get("surfaces_differ") else 40

    # taste: Ford vote
    pref = (taste.get("preference") or "").lower()
    if pref == "chamber":
        scores["taste"] = 90
    elif pref == "prod":
        scores["taste"] = 25
    elif pref == "tie":
        scores["taste"] = 55
    else:
        scores["taste"] = 50  # abstain / no vote

    overall = _clamp(sum(scores.values()) / max(1, len(scores)))

    # Verdict
    if not health["chamber_up"]:
        verdict = "chamber_down"
        narrative = "Chamber is unreachable. Fix L2 hosting before scoring product taste."
    elif not health["prod_up"] or not health["prod_gate_clean"]:
        verdict = "prod_unhealthy"
        narrative = (
            "Production is down or shows the Private Preview gate. "
            "Abort thrash; restore prod first."
        )
    elif pref == "chamber" and overall >= 55:
        verdict = "chamber_preferred"
        narrative = (
            "Ford prefers the chamber and automatic scores support a real delta. "
            "Consider promoting chamber wins to main."
        )
    elif pref == "prod":
        verdict = "prod_preferred"
        narrative = (
            "Ford still prefers production. Chamber thrash isn't landing as product taste yet. "
            "Sharpen the drive prism or pick smaller vacuums."
        )
    elif n_jobs + n_commits == 0:
        verdict = "idle"
        narrative = "No sandbox ships this window. Chamber is only a mirror of prod baseline."
    elif health.get("surfaces_differ") and overall >= 55:
        verdict = "chamber_moving"
        narrative = (
            "Chamber surface differs from prod and activity is present. "
            "Taste vote still needed for a launch decision."
        )
    else:
        verdict = "mixed"
        narrative = (
            "Close or incomplete. Open chamber + prod side-by-side for 10 minutes "
            "and cast a taste vote."
        )

    card = {
        "ok": True,
        "level": "L4",
        "generated_at": _iso(),
        "window_days": days,
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "urls": {"chamber": chamber_url, "prod": prod_url},
        "health": health,
        "run": run_meta,
        "sandbox_scorecard": {
            "overall": (sandbox_card or {}).get("overall"),
            "verdict": (sandbox_card or {}).get("verdict"),
            "counts": (sandbox_card or {}).get("counts"),
            "narrative": (sandbox_card or {}).get("narrative"),
        }
        if sandbox_card
        else None,
        "reality_ships": reality_ships[:30],
        "taste": taste or {"preference": "abstain", "note": "no vote yet"},
        "scores": scores,
        "overall": overall,
        "verdict": verdict,
        "narrative": narrative,
        "how_to_score_taste": (
            "Open chamber and prod for 10 minutes. POST /admin/sovereign/chamber/taste "
            "with preference=chamber|prod|tie and an optional note."
        ),
    }
    return card


def scorecard_markdown(card: dict[str, Any]) -> str:
    if not card:
        return "# Chamber scorecard\n\n(empty)\n"
    s = card.get("scores") or {}
    h = card.get("health") or {}
    lines = [
        f"# Chamber vs Prod scorecard (L4)",
        "",
        f"**Generated:** {card.get('generated_at')}",
        f"**Window:** {card.get('window_days')}d",
        f"**Verdict:** `{card.get('verdict')}` · overall **{card.get('overall')}/100**",
        "",
        card.get("narrative") or "",
        "",
        "## URLs",
        f"- Chamber: { (card.get('urls') or {}).get('chamber') }",
        f"- Prod: { (card.get('urls') or {}).get('prod') }",
        "",
        "## Health",
        f"- Chamber up: {h.get('chamber_up')} ({(h.get('chamber') or {}).get('status')}, "
        f"{(h.get('chamber') or {}).get('bytes')} bytes)",
        f"- Prod up: {h.get('prod_up')} · gate clean: {h.get('prod_gate_clean')}",
        f"- Surfaces differ: {h.get('surfaces_differ')} · byte Δ: {h.get('byte_delta')}",
        "",
        "## Scores",
    ]
    for k, v in s.items():
        lines.append(f"- **{k}**: {v}")
    taste = card.get("taste") or {}
    lines += [
        "",
        "## Taste",
        f"- Preference: `{taste.get('preference', 'abstain')}`",
        f"- Note: {taste.get('note') or '—'}",
        f"- At: {taste.get('at') or '—'}",
        "",
        "## Sandbox activity",
    ]
    run = card.get("run") or {}
    lines.append(
        f"- Run `{run.get('id')}` · jobs={run.get('sovereign_jobs')} "
        f"commits={run.get('sovereign_commits')} chamber_deploys={run.get('chamber_deploys')}"
    )
    sc = card.get("sandbox_scorecard") or {}
    if sc:
        lines.append(f"- Sandbox verdict: `{sc.get('verdict')}` overall {sc.get('overall')}")
    lines += ["", "## Reality ships (window)"]
    for sh in card.get("reality_ships") or []:
        lines.append(f"- [{sh.get('source')}] {sh.get('title')}")
    if not card.get("reality_ships"):
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def run_and_persist_scorecard(db=None, *, days: int = 7) -> dict[str, Any]:
    """Build scorecard, write to disk + memory, return card + markdown."""
    card = build_chamber_scorecard(db, days=days)
    md = scorecard_markdown(card)

    # Disk under docs/sovereign/chamber/
    out_dir = SO_ROOT / "docs" / "sovereign" / "chamber"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = _utcnow().strftime("%Y%m%d")
        (out_dir / "scorecard_latest.json").write_text(
            json.dumps(card, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (out_dir / "scorecard_latest.md").write_text(md, encoding="utf-8")
        (out_dir / f"scorecard_{ts}.json").write_text(
            json.dumps(card, indent=2, default=str) + "\n", encoding="utf-8"
        )
        card["paths"] = {
            "json": str(out_dir / "scorecard_latest.json"),
            "md": str(out_dir / "scorecard_latest.md"),
        }
    except Exception as e:  # noqa: BLE001
        card["disk_warn"] = str(e)[:200]

    if db is not None:
        try:
            from .energy_agent_sovereign import memory_set, memory_get_all, write_note

            memory_set(
                db,
                SCORECARD_KEY,
                json.dumps(
                    {
                        "at": card.get("generated_at"),
                        "overall": card.get("overall"),
                        "verdict": card.get("verdict"),
                        "scores": card.get("scores"),
                        "urls": card.get("urls"),
                    }
                ),
                source="chamber_score",
            )
            # history (cap 20)
            hist: list = []
            for m in memory_get_all(db, limit=40):
                if m.get("key") == SCORE_HISTORY_KEY and m.get("value"):
                    try:
                        hist = json.loads(m["value"])
                    except json.JSONDecodeError:
                        hist = []
                    break
            if not isinstance(hist, list):
                hist = []
            hist.append(
                {
                    "at": card.get("generated_at"),
                    "overall": card.get("overall"),
                    "verdict": card.get("verdict"),
                    "taste": (card.get("taste") or {}).get("preference"),
                }
            )
            memory_set(
                db,
                SCORE_HISTORY_KEY,
                json.dumps(hist[-20:]),
                source="chamber_score",
            )
            write_note(
                db,
                kind="memory",
                title=f"Chamber scorecard {card.get('verdict')} ({card.get('overall')})",
                body=md[:6000],
                provider="chamber_score",
            )
        except Exception as e:  # noqa: BLE001
            card["memory_warn"] = str(e)[:200]

    return {"ok": True, "scorecard": card, "markdown": md}


def latest_scorecard_summary(db=None) -> dict[str, Any]:
    """Lightweight summary for weekly digest / portal badge."""
    # Prefer disk, then memory
    p = SO_ROOT / "docs" / "sovereign" / "chamber" / "scorecard_latest.json"
    if p.is_file():
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
            return {
                "ok": True,
                "overall": card.get("overall"),
                "verdict": card.get("verdict"),
                "generated_at": card.get("generated_at"),
                "urls": card.get("urls"),
                "taste": (card.get("taste") or {}).get("preference"),
            }
        except Exception:
            pass
    if db is not None:
        try:
            from .energy_agent_sovereign import memory_get_all

            for m in memory_get_all(db, limit=40):
                if m.get("key") == SCORECARD_KEY and m.get("value"):
                    return {"ok": True, **json.loads(m["value"])}
        except Exception:
            pass
    return {"ok": False, "error": "no_scorecard_yet"}
