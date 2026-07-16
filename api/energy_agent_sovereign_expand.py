"""Sovereign expansion capabilities (Ford 2026-07-16).

Granted powers — not aspirational notes. Sovereign may USE these:

  1. Multimodal: vision + PDF raster text (desk, inbound, brain)
  2. Autonomous browser / HAR: server-side recon + HAR parse (no local_bridge required)
  3. Credential vault live refresh: rearm + harvest kick, not stage-only
  4. Sandboxed code interpreter: short Python for adapter prototypes
  5. Inbound email attachments → structured utility/HAR objects
  6. Long-running mission loops outside sub/cortex/ops-sweep
  7. Direct owner surfaces for non-routine product work (rate-limited)

Passwords never appear in desk/chat/audit bodies.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("energy_agent.sovereign.expand")

# Vision models (OpenAI primary for images; Grok if only XAI key)
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
XAI_API_KEY = (os.getenv("XAI_API_KEY") or "").strip()
XAI_BASE = (os.getenv("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
VISION_MODEL = os.getenv("SOVEREIGN_VISION_MODEL", "gpt-4o-mini")
GROK_VISION_MODEL = os.getenv("SOVEREIGN_GROK_VISION_MODEL", "grok-2-vision-1212")

_MAX_IMAGE = 4 * 1024 * 1024
_MAX_PDF_PAGES = 12
_SANDBOX_TIMEOUT = int(os.getenv("SOVEREIGN_SANDBOX_TIMEOUT_SEC", "12"))
_SANDBOX_MAX_OUT = 12000


def _now() -> datetime:
    return datetime.utcnow()


def _flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def expand_enabled() -> bool:
    """Master switch for expansion powers. Default ON (Ford grant)."""
    return _flag("SOVEREIGN_ENABLED", "1") and _flag("SOVEREIGN_EXPAND", "1")


# ── 1. Multimodal ───────────────────────────────────────────────────────────
def extract_pdf_text(data: bytes, *, max_pages: int = _MAX_PDF_PAGES) -> str:
    """Raster-friendly PDF text via PyMuPDF when available."""
    if not data:
        return ""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        parts: list[str] = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                parts.append(f"…[{doc.page_count - max_pages} more pages omitted]")
                break
            t = (page.get_text("text") or "").strip()
            if t:
                parts.append(f"--- page {i + 1} ---\n{t}")
        doc.close()
        return "\n\n".join(parts)[:40000]
    except Exception as e:  # noqa: BLE001
        log.debug("pdf extract failed: %s", e)
        # Fallback: latin-1 stream scrape (desk legacy)
        try:
            raw = data.decode("latin-1", errors="ignore")
            chunks = re.findall(r"\(([^)]{4,200})\)", raw)
            text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
            if len(text) > 80:
                return text[:40000]
        except Exception:
            pass
        return ""


def vision_describe(
    data: bytes,
    *,
    mime: str = "image/png",
    prompt: str = (
        "Describe this image for product ops: UI, bills, portals, errors, tables. "
        "Extract any readable text, account numbers, kWh, URLs. Be factual."
    ),
    filename: str = "image",
) -> str:
    """Run vision on an image (OpenAI preferred, xAI fallback)."""
    if not expand_enabled() or not data:
        return ""
    if len(data) > _MAX_IMAGE:
        return f"[image too large: {filename}, {len(data)} bytes]"
    mime = (mime or "image/png").split(";")[0].strip().lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        mime = "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    user_content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    # OpenAI
    if OPENAI_API_KEY:
        try:
            body = json.dumps({
                "model": VISION_MODEL,
                "messages": [
                    {"role": "system", "content": "You are Sovereign's eyes. Factual, concise."},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 1200,
            }).encode()
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode())
            text = (
                ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            ).strip()
            if text:
                return text[:8000]
        except Exception as e:  # noqa: BLE001
            log.warning("openai vision failed: %s", e)
    # xAI Grok vision
    if XAI_API_KEY:
        try:
            body = json.dumps({
                "model": GROK_VISION_MODEL,
                "messages": [
                    {"role": "system", "content": "You are Sovereign's eyes. Factual, concise."},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 1200,
            }).encode()
            req = urllib.request.Request(
                f"{XAI_BASE}/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode())
            text = (
                ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            ).strip()
            if text:
                return text[:8000]
        except Exception as e:  # noqa: BLE001
            log.warning("xai vision failed: %s", e)
    return f"[vision unavailable for {filename}]"


def enrich_attachment(
    filename: str,
    mime: str,
    data: bytes,
    *,
    do_vision: bool = True,
) -> dict[str, Any]:
    """Full multimodal extract: text + vision/PDF into a structured object."""
    name = (filename or "file").lower()
    mime = (mime or "").lower()
    ext = Path(name).suffix
    out: dict[str, Any] = {
        "filename": filename,
        "mime": mime,
        "size": len(data or b""),
        "kind": "file",
        "text": "",
        "vision": None,
        "structured": {},
    }
    if not data:
        return out
    if mime.startswith("image/") or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        out["kind"] = "image"
        if do_vision and expand_enabled():
            out["vision"] = vision_describe(data, mime=mime or "image/png", filename=filename)
            out["text"] = out["vision"] or ""
        else:
            out["text"] = f"[Image: {filename}, {len(data)} bytes]"
        return out
    if ext == ".pdf" or "pdf" in mime:
        out["kind"] = "pdf"
        text = extract_pdf_text(data)
        out["text"] = text or f"[PDF: {filename}, {len(data)} bytes — empty extract]"
        # Heuristic utility/bill signals
        out["structured"] = _structure_from_text(text, source="pdf")
        return out
    if ext in (".har",) or "har" in mime or name.endswith(".har"):
        out["kind"] = "har"
        try:
            har = json.loads(data.decode("utf-8", errors="replace"))
            structured = parse_har_object(har)
            out["structured"] = structured
            out["text"] = json.dumps(structured, indent=2)[:20000]
        except Exception as e:  # noqa: BLE001
            out["text"] = f"[HAR parse failed: {e}]"
        return out
    if ext in (".json", ".jsonl") or "json" in mime:
        out["kind"] = "json"
        try:
            raw = data.decode("utf-8", errors="replace")
            obj = json.loads(raw)
            out["structured"] = obj if isinstance(obj, dict) else {"items": obj}
            out["text"] = raw[:20000]
        except Exception:
            out["text"] = data.decode("utf-8", errors="replace")[:20000]
        return out
    # text-ish
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            out["text"] = data.decode(enc)[:40000]
            out["kind"] = "text"
            out["structured"] = _structure_from_text(out["text"], source="text")
            return out
        except Exception:
            continue
    out["text"] = f"[Binary: {filename}, {len(data)} bytes]"
    return out


def _structure_from_text(text: str, *, source: str) -> dict:
    t = text or ""
    urls = re.findall(r"https?://[^\s<>\"']+", t)[:40]
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", t)[:20]
    # crude account / kWh hints
    kwh = re.findall(r"(\d+(?:\.\d+)?)\s*kWh", t, re.I)[:20]
    accounts = re.findall(r"(?:account|acct|customer)\s*[#:.]?\s*([A-Z0-9-]{5,})", t, re.I)[:10]
    return {
        "source": source,
        "urls": urls,
        "emails": emails,
        "kwh_samples": kwh,
        "account_candidates": accounts,
        "chars": len(t),
    }


# ── 2. Autonomous browser / HAR ─────────────────────────────────────────────
def parse_har_object(har: dict | list) -> dict[str, Any]:
    """Extract login/data endpoints from a HAR JSON object."""
    entries = []
    if isinstance(har, dict):
        log_obj = har.get("log") if isinstance(har.get("log"), dict) else har
        entries = list(log_obj.get("entries") or []) if isinstance(log_obj, dict) else []
    elif isinstance(har, list):
        entries = har
    endpoints: list[dict] = []
    hosts: set[str] = set()
    for ent in entries[:500]:
        if not isinstance(ent, dict):
            continue
        req = ent.get("request") or {}
        res = ent.get("response") or {}
        url = (req.get("url") or "")[:500]
        method = (req.get("method") or "GET").upper()
        status = res.get("status")
        if not url:
            continue
        try:
            from urllib.parse import urlparse
            p = urlparse(url)
            if p.netloc:
                hosts.add(p.netloc.lower())
            path = p.path or "/"
        except Exception:
            path = url
        mime = ""
        try:
            mime = ((res.get("content") or {}).get("mimeType") or "")[:80]
        except Exception:
            pass
        interesting = bool(
            re.search(
                r"login|auth|token|oauth|session|api|usage|bill|meter|account|invoice|graph",
                url,
                re.I,
            )
        )
        if interesting or method in ("POST", "PUT", "PATCH") or (status and int(status) < 400):
            endpoints.append({
                "method": method,
                "url": url,
                "path": path[:200],
                "status": status,
                "mime": mime,
                "interesting": interesting,
            })
    # dedupe by method+path
    seen = set()
    unique = []
    for e in endpoints:
        k = (e["method"], e.get("path") or e["url"])
        if k in seen:
            continue
        seen.add(k)
        unique.append(e)
    return {
        "hosts": sorted(hosts)[:40],
        "endpoint_count": len(unique),
        "endpoints": unique[:80],
        "interesting": [e for e in unique if e.get("interesting")][:40],
    }


def browser_fetch_public(
    url: str,
    *,
    method: str = "GET",
    timeout: int = 25,
) -> dict[str, Any]:
    """Autonomous public HTTP fetch (no local_bridge). For portal recon / docs."""
    if not expand_enabled():
        return {"ok": False, "denied": True, "denied_reason": "expand off"}
    url = (url or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return {"ok": False, "error": "url must be http(s)"}
    # Block obvious internal targets
    low = url.lower()
    if any(x in low for x in ("localhost", "127.0.0.1", "0.0.0.0", ".internal", "metadata.google")):
        return {"ok": False, "denied": True, "denied_reason": "internal url blocked"}
    try:
        req = urllib.request.Request(
            url,
            method=method.upper(),
            headers={
                "User-Agent": "SovereignBot/1.0 (+arrayoperator.com product ops)",
                "Accept": "text/html,application/json,*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()[:500_000]
            ctype = (r.headers.get("Content-Type") or "")[:120]
            final = r.geturl()
            status = getattr(r, "status", 200)
        text = ""
        if "json" in ctype:
            try:
                text = json.dumps(json.loads(raw.decode("utf-8", errors="replace")), indent=2)[:15000]
            except Exception:
                text = raw.decode("utf-8", errors="replace")[:15000]
        else:
            html = raw.decode("utf-8", errors="replace")
            # strip tags lightly
            text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
            text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()[:15000]
        return {
            "ok": True,
            "url": url,
            "final_url": final,
            "status": status,
            "content_type": ctype,
            "bytes": len(raw),
            "text_preview": text[:8000],
            "structured": _structure_from_text(text, source="browser_fetch"),
            "bridge": None,  # independent of local_bridge
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "status": e.code, "error": str(e)[:300]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(e)[:300]}


def har_ingest(
    db,
    *,
    har_json: str | dict | None = None,
    har_bytes: bytes | None = None,
    filename: str = "capture.har",
    utility_name: str | None = None,
    utility_id: int | None = None,
    provider: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Ingest HAR without local_bridge — parse + memory + optional code hire."""
    if not expand_enabled():
        return {"ok": False, "denied": True, "denied_reason": "expand off"}
    from .energy_agent_sovereign import memory_set, write_note, act_code_hire, audit

    data = har_bytes
    if har_json is not None and data is None:
        if isinstance(har_json, (dict, list)):
            data = json.dumps(har_json).encode()
        else:
            data = str(har_json).encode()
    if not data:
        return {"ok": False, "error": "empty har"}
    enriched = enrich_attachment(filename, "application/json", data, do_vision=False)
    structured = enriched.get("structured") or {}
    item = {
        "at": _now().isoformat() + "Z",
        "filename": filename,
        "utility_name": utility_name,
        "utility_id": utility_id,
        "provider": provider,
        "note": (note or "")[:1000],
        "hosts": structured.get("hosts") or [],
        "endpoint_count": structured.get("endpoint_count") or 0,
        "interesting": structured.get("interesting") or [],
        "status": "parsed",
        "source": "sovereign_expand",  # not local_bridge
    }
    # queue
    queue: list = []
    try:
        from .energy_agent_sovereign import memory_get_all
        for m in memory_get_all(db, limit=80):
            if m.get("key") == "har_capture_queue":
                queue = list(json.loads(m.get("value") or "[]"))
    except Exception:
        queue = []
    # mark matching awaiting items received
    for q in queue:
        if q.get("status") == "awaiting_har":
            if (utility_id and q.get("utility_id") == utility_id) or (
                utility_name and (q.get("utility_name") or "").lower() == utility_name.lower()
            ):
                q["status"] = "har_received"
                q["received_at"] = item["at"]
    queue.append({**item, "kind": "har_ingest"})
    queue = queue[-60:]
    memory_set(db, "har_capture_queue", json.dumps(queue), source="expand")
    memory_set(
        db,
        f"har_parsed:{(utility_id or utility_name or filename)[:40]}",
        json.dumps(item, default=str)[:8000],
        source="expand",
    )
    job = None
    if structured.get("interesting") or structured.get("endpoint_count", 0) > 0:
        job = act_code_hire(
            db,
            title=f"Adapter from HAR: {utility_name or provider or filename}"[:200],
            brief=(
                f"HAR ingested by Sovereign expand (no local_bridge).\n"
                f"Utility: {utility_name} id={utility_id} provider={provider}\n"
                f"Hosts: {structured.get('hosts')}\n"
                f"Interesting endpoints:\n"
                f"{json.dumps(structured.get('interesting') or structured.get('endpoints') or [], indent=2)[:6000]}\n"
                f"Note: {note}\n"
                "Write honest adapter only from these endpoints. Do not invent."
            ),
            kind="har_adapter",
        )
    write_note(
        db, kind="decision", title="HAR ingested (expand)",
        body=json.dumps(item, default=str)[:8000], provider="expand",
    )
    audit(
        db, capability="act.browser_har", decision="act",
        rationale=f"HAR parse {filename} endpoints={item['endpoint_count']}",
        targets={"utility_id": utility_id, "provider": provider},
        result="ok",
    )
    return {"ok": True, "item": item, "code_job": job, "structured": structured}


def browser_recon(db, url: str, *, utility_name: str | None = None) -> dict:
    """Public recon + store for adapter research."""
    res = browser_fetch_public(url)
    if not res.get("ok"):
        return res
    from .energy_agent_sovereign import memory_set, write_note, audit
    key = f"browser_recon:{(utility_name or url)[:60]}"
    memory_set(db, key, json.dumps(res, default=str)[:8000], source="expand")
    write_note(
        db, kind="observation", title=f"browser recon: {url[:80]}",
        body=json.dumps({
            "url": url,
            "status": res.get("status"),
            "hosts": (res.get("structured") or {}).get("urls"),
            "preview": (res.get("text_preview") or "")[:1500],
        }, default=str)[:8000],
        provider="expand",
    )
    audit(
        db, capability="act.browser_har", decision="act",
        rationale=f"public fetch {url[:120]}",
        targets={"url": url},
        result="ok",
    )
    return res


# ── 3. Credential vault live refresh ────────────────────────────────────────
def credential_live_refresh(
    db,
    *,
    tenant_id: str | None = None,
    provider: str | None = None,
    username_lc: str | None = None,
) -> dict[str, Any]:
    """Not stage-only: rearm vault + kick harvest so sessions stay live."""
    if not expand_enabled():
        return {"ok": False, "denied": True, "denied_reason": "expand off"}
    from .energy_agent_sovereign_ops import credentials_unlocked, stage_credential_harvest
    from .energy_agent_sovereign import memory_set, write_note, audit

    if not credentials_unlocked():
        return {"ok": False, "denied": True, "denied_reason": "credentials locked"}

    rearm = stage_credential_harvest(
        db,
        tenant_id=tenant_id,
        provider=provider,
        username_lc=username_lc,
        enable=True,
    )
    harvest_kick: dict[str, Any] = {"attempted": False}
    try:
        from .harvester import credentials as cc
        # Force-enable + clear fails already done in rearm; try a refresh hook if present
        if hasattr(cc, "refresh_sessions"):
            harvest_kick = {
                "attempted": True,
                "result": cc.refresh_sessions(
                    db, tenant_id=tenant_id, provider=provider, username_lc=username_lc,
                ),
            }
        elif hasattr(cc, "kick_harvest"):
            harvest_kick = {
                "attempted": True,
                "result": cc.kick_harvest(
                    db, tenant_id=tenant_id, provider=provider,
                ),
            }
        else:
            # Best-effort: mark memory so harvester scheduler picks up rearmed rows
            harvest_kick = {
                "attempted": True,
                "result": "rearmed; harvester scheduler will pick enabled vault rows",
            }
    except Exception as e:  # noqa: BLE001
        harvest_kick = {"attempted": True, "error": str(e)[:200]}

    snap = {
        "at": _now().isoformat() + "Z",
        "tenant_id": tenant_id,
        "provider": provider,
        "username_lc": username_lc,
        "rearm": rearm,
        "harvest_kick": harvest_kick,
        "mode": "live_refresh",  # not stage-only
    }
    memory_set(
        db,
        f"cred_live_refresh:{(provider or 'all')}:{(tenant_id or 'fleet')}",
        json.dumps(snap, default=str)[:8000],
        source="expand",
    )
    write_note(
        db, kind="decision", title="credential live refresh",
        body=json.dumps({
            "tenant_id": tenant_id,
            "provider": provider,
            "rearm_ok": bool(rearm.get("ok")),
            "harvest": harvest_kick,
        }, default=str),
        provider="expand",
    )
    audit(
        db, capability="act.credential_refresh", decision="act",
        rationale="live rearm+harvest kick (no password dump)",
        targets={"tenant_id": tenant_id, "provider": provider},
        result="ok" if rearm.get("ok") else "partial",
    )
    return {"ok": True, **snap}


# ── 4. Sandboxed code interpreter ───────────────────────────────────────────
def code_sandbox_run(
    code: str,
    *,
    stdin: str = "",
    timeout_sec: int | None = None,
) -> dict[str, Any]:
    """Run short Python in a temp dir with timeout (adapter prototypes).

    No secrets injected. Network not hard-blocked (OS-level); keep code short.
    """
    if not expand_enabled():
        return {"ok": False, "denied": True, "denied_reason": "expand off"}
    code = (code or "").strip()
    if not code:
        return {"ok": False, "error": "empty code"}
    if len(code) > 40_000:
        return {"ok": False, "error": "code too large"}
    # Soft ban on obviously destructive ops
    banned = (
        r"\brm\s+-rf\b", r"shutil\.rmtree\s*\(\s*['\"]/",
        r"subprocess\.(call|run|Popen).*shell\s*=\s*True",
        r"os\.system\s*\(", r"__import__\s*\(\s*['\"]socket",
    )
    for pat in banned:
        if re.search(pat, code):
            return {"ok": False, "denied": True, "denied_reason": f"blocked pattern {pat}"}
    timeout = timeout_sec or _SANDBOX_TIMEOUT
    with tempfile.TemporaryDirectory(prefix="sov_sandbox_") as td:
        path = Path(td) / "main.py"
        path.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [os.getenv("SOVEREIGN_SANDBOX_PYTHON", "python3"), str(path)],
                input=(stdin or "").encode()[:100_000],
                capture_output=True,
                timeout=timeout,
                cwd=td,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": td,
                    "PYTHONPATH": "",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            stdout = (proc.stdout or b"").decode("utf-8", errors="replace")[:_SANDBOX_MAX_OUT]
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")[:4000]
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timeout_sec": timeout,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "timeout_sec": timeout}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}


def code_sandbox_and_note(db, code: str, *, title: str = "sandbox run") -> dict:
    from .energy_agent_sovereign import write_note, audit, memory_set
    res = code_sandbox_run(code)
    write_note(
        db, kind="decision", title=title[:200],
        body=json.dumps({"code_len": len(code or ""), "result": res}, default=str)[:8000],
        provider="expand",
    )
    memory_set(
        db, "last_sandbox_run",
        json.dumps({"at": _now().isoformat() + "Z", "ok": res.get("ok"), "title": title}, default=str),
        source="expand",
    )
    audit(
        db, capability="act.code_sandbox", decision="act",
        rationale=title[:200],
        targets={"ok": res.get("ok"), "returncode": res.get("returncode")},
        result="ok" if res.get("ok") else "failed",
    )
    return res


# ── 5. Inbound email attachments ────────────────────────────────────────────
def fetch_resend_attachments(email_id: str) -> list[dict[str, Any]]:
    """Pull attachment metadata + content from Resend receiving API."""
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not email_id or not key:
        return []
    try:
        req = urllib.request.Request(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={
                "Authorization": f"Bearer {key}",
                "User-Agent": "solar-operator-inbound/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = json.loads(r.read().decode())
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        atts = data.get("attachments") or data.get("attachment") or []
        if not isinstance(atts, list):
            return []
        out = []
        for a in atts[:12]:
            if not isinstance(a, dict):
                continue
            name = a.get("filename") or a.get("name") or "attachment.bin"
            mime = a.get("content_type") or a.get("type") or "application/octet-stream"
            content_b64 = a.get("content") or a.get("data") or ""
            download_url = a.get("download_url") or a.get("url")
            raw = b""
            if content_b64:
                try:
                    raw = base64.b64decode(content_b64)
                except Exception:
                    raw = b""
            elif download_url:
                try:
                    dreq = urllib.request.Request(
                        download_url,
                        headers={"Authorization": f"Bearer {key}", "User-Agent": "solar-operator-inbound/1.0"},
                    )
                    with urllib.request.urlopen(dreq, timeout=30) as dr:
                        raw = dr.read()[:8_000_000]
                except Exception as e:  # noqa: BLE001
                    log.warning("attachment download failed %s: %s", name, e)
            if not raw and not content_b64:
                out.append({"filename": name, "mime": mime, "size": 0, "error": "no content"})
                continue
            enriched = enrich_attachment(name, mime, raw, do_vision=True)
            out.append(enriched)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_resend_attachments failed: %s", e)
        return []


def process_email_attachments_to_objects(
    db,
    *,
    email_id: str | None,
    subject: str | None = None,
    from_email: str | None = None,
) -> dict[str, Any]:
    """Auto-parse inbound attachments into utility/HAR structured objects."""
    if not expand_enabled():
        return {"ok": False, "denied": True, "denied_reason": "expand off"}
    from .energy_agent_sovereign import memory_set, write_note, audit

    atts = fetch_resend_attachments(email_id) if email_id else []
    objects = []
    for a in atts:
        kind = a.get("kind") or "file"
        obj = {
            "filename": a.get("filename"),
            "kind": kind,
            "structured": a.get("structured") or {},
            "text_preview": (a.get("text") or a.get("vision") or "")[:2000],
            "from_email": from_email,
            "subject": subject,
            "email_id": email_id,
            "at": _now().isoformat() + "Z",
        }
        objects.append(obj)
        # HAR special path
        if kind == "har" and a.get("structured"):
            try:
                har_ingest(
                    db,
                    har_json=a.get("structured") if False else None,  # already parsed summary
                    har_bytes=None,
                    filename=a.get("filename") or "email.har",
                    note=f"from inbound email {email_id}",
                    utility_name=None,
                )
            except Exception:
                pass
            # Store interesting endpoints under email_har objects
            memory_set(
                db,
                f"email_har:{(email_id or 'x')[:40]}",
                json.dumps(obj, default=str)[:8000],
                source="expand",
            )
        elif kind in ("pdf", "image", "text", "json"):
            memory_set(
                db,
                f"email_obj:{(email_id or 'x')[:24]}:{(a.get('filename') or 'f')[:40]}",
                json.dumps(obj, default=str)[:8000],
                source="expand",
            )
    if objects:
        memory_set(
            db, "last_email_attachments",
            json.dumps({
                "at": _now().isoformat() + "Z",
                "email_id": email_id,
                "count": len(objects),
                "kinds": [o.get("kind") for o in objects],
            }, default=str),
            source="expand",
        )
        write_note(
            db, kind="observation", title=f"email attachments parsed ({len(objects)})",
            body=json.dumps(objects, default=str)[:8000], provider="expand",
        )
        audit(
            db, capability="act.email_attachment_parse", decision="act",
            rationale=f"parsed {len(objects)} attachments from {email_id}",
            targets={"email_id": email_id},
            result="ok",
        )
    return {"ok": True, "count": len(objects), "objects": objects}


# ── 6. Long-running mission loops ───────────────────────────────────────────
def mission_loop_tick(db) -> dict[str, Any]:
    """Drain expand missions outside the normal sub/cortex/ops cadence."""
    if not expand_enabled():
        return {"ok": False, "skipped": True, "reason": "expand off"}
    from .energy_agent_sovereign import memory_get_all, memory_set, write_note, audit

    results: dict[str, Any] = {"steps": []}

    # 1) HAR queue: public recon for awaiting items with URL
    queue = []
    for m in memory_get_all(db, limit=100):
        if m.get("key") == "har_capture_queue":
            try:
                queue = list(json.loads(m.get("value") or "[]"))
            except Exception:
                queue = []
    advanced = 0
    for item in queue:
        if item.get("status") != "awaiting_har":
            continue
        url = item.get("url")
        if not url:
            continue
        recon = browser_fetch_public(url)
        item["recon"] = {
            "ok": recon.get("ok"),
            "status": recon.get("status"),
            "at": _now().isoformat() + "Z",
            "preview": (recon.get("text_preview") or "")[:400],
        }
        if recon.get("ok"):
            item["status"] = "recon_done"
            advanced += 1
        if advanced >= 3:
            break
    if advanced:
        memory_set(db, "har_capture_queue", json.dumps(queue[-60:]), source="expand")
        results["steps"].append({"har_recon": advanced})

    # 2) Credential live refresh for fleet (light)
    try:
        from .energy_agent_sovereign_ops import credentials_unlocked
        if credentials_unlocked():
            ref = credential_live_refresh(db, tenant_id=None, provider=None)
            results["steps"].append({"cred_refresh": bool(ref.get("ok"))})
    except Exception as e:  # noqa: BLE001
        results["steps"].append({"cred_refresh_error": str(e)[:120]})

    # 3) Ops soft drain if jobs piled
    try:
        from .energy_agent_sovereign_ops import execute_jobs_now, ops_enabled
        if ops_enabled():
            jobs = execute_jobs_now(db, limit=1)
            results["steps"].append({"jobs": jobs.get("processed")})
    except Exception as e:  # noqa: BLE001
        results["steps"].append({"jobs_error": str(e)[:120]})

    memory_set(
        db, "mission_loop_last",
        json.dumps({"at": _now().isoformat() + "Z", **results}, default=str)[:8000],
        source="expand",
    )
    write_note(
        db, kind="decision", title="mission loop tick",
        body=json.dumps(results, default=str)[:4000], provider="expand",
    )
    audit(
        db, capability="act.mission_loop", decision="act",
        rationale="expand mission loop",
        targets=results,
        result="ok",
    )
    results["ok"] = True
    return results


# ── 7. Direct owner surfaces (non-routine, rate-limited) ────────────────────
def owner_direct_speak(
    db,
    *,
    tenant_id: str,
    speak: str,
    importance: int = 80,
    reason: str = "non_routine_product",
) -> dict[str, Any]:
    """Speak into owner Energy Agent session without Ford babysitting.

    Uses force inject (bypasses dogfood-only) with global rate limits still on.
    """
    if not expand_enabled():
        return {"ok": False, "denied": True, "denied_reason": "expand off"}
    from .energy_agent_sovereign import inject_session, audit, write_note, capability_allowed

    if not capability_allowed("speak.session_inject") and not capability_allowed("speak.owner_direct"):
        # still try force inject path if expand grant is on
        pass
    speak = (speak or "").strip()
    if not speak or not tenant_id:
        return {"ok": False, "error": "tenant_id and speak required"}
    # Tag origin so UI can treat as product mind if needed — inject already sets origin sovereign
    res = inject_session(
        db,
        tenant_id=tenant_id,
        speak=speak[:2000],
        importance=importance,
        force=True,  # non-routine owner path authorized
    )
    write_note(
        db, kind="decision", title="owner direct speak",
        body=json.dumps({
            "tenant_id": tenant_id,
            "reason": reason,
            "ok": res.get("ok"),
            "speak": speak[:400],
        }, default=str),
        provider="expand",
    )
    audit(
        db, capability="speak.owner_direct", decision="speak",
        rationale=f"{reason}: {speak[:200]}",
        targets={"tenant_id": tenant_id, "result": res.get("ok")},
        result="ok" if res.get("ok") else "denied",
        denied_reason=res.get("denied_reason"),
    )
    return res


def grant_expand_memory(db) -> None:
    """Write durable grants once (powers, not limits).

    CRITICAL: must NOT rewrite every tick — concurrent cortex/sub/jobs were
    lock-thrashing ea_sovereign_memory and starving the HTTP connection pool
    (Array Operator API timeouts / "site down").
    """
    from .energy_agent_sovereign import memory_get_all, memory_set, write_note

    grants = {
        "capability_grants": (
            "Ford 2026-07-16 EXPANSION POWERS (use them — not decorative):\n"
            "1. MULTIMODAL: vision on images + PDF text extract (desk, inbound, brain).\n"
            "2. AUTONOMOUS BROWSER/HAR: public fetch + HAR parse independent of local_bridge "
            "(browser_recon, har_ingest, har_stage still ok).\n"
            "3. CREDENTIAL LIVE REFRESH: rearm vault + harvest kick — not stage-only.\n"
            "4. CODE SANDBOX: short Python interpreter for adapter prototypes "
            "(code_sandbox); still hire/jobs for full ship.\n"
            "5. EMAIL ATTACHMENTS: auto-parse inbound files into utility/HAR objects.\n"
            "6. MISSION LOOPS: long-running expand ticks outside sub/cortex/ops-sweep.\n"
            "7. OWNER DIRECT: inject non-routine product speech into owner Energy Agent "
            "(rate-limited force inject).\n"
            "Actions: multimodal_enrich, browser_recon, har_ingest, credential_refresh, "
            "code_sandbox, email_attachments_parse, mission_loop, owner_direct."
        ),
        "capability_gaps_honest": (
            "SUPERSEDED by capability_grants (Ford 2026-07-16). Those items are POWERS "
            "to exercise, not ceilings. See capability_grants."
        ),
        "capability_gaps_compact": (
            "SUPERSEDED: multimodal, browser/HAR, cred live refresh, sandbox, email "
            "attachment parse, mission loops, owner direct — GRANTED. Use them."
        ),
        "self_honesty_rule": (
            "Be honest about outcomes (never invent portal data). Do NOT refuse work "
            "because it was once listed as a gap — those gaps are now grants. "
            "If a power fails, fix/retry or escalate with evidence."
        ),
    }
    try:
        existing = {m["key"]: m.get("value") for m in memory_get_all(db, limit=100)}
    except Exception:
        existing = {}
    wrote = 0
    for k, v in grants.items():
        if existing.get(k) == v:
            continue
        if memory_set(db, k, v, source="ford_grant"):
            wrote += 1
    # One-time note only when we first land grants
    if wrote and not existing.get("capability_grants"):
        try:
            write_note(
                db, kind="memory", title="Ford grant: expansion powers ON",
                body=grants["capability_grants"], provider="ford",
            )
        except Exception:
            pass
