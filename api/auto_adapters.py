#!/usr/bin/env python3
"""Self-improving adapter engine + registry, exposed as a FastAPI router.

A GENERIC alternative to per-portal content scripts: the extension captures a raw
portal response, the engine fingerprints the platform, and either serves a cached
declarative adapter or SYNTHESIZES one (cheap heuristic -> Claude agent), validates
it by reconciliation (or structural checks when no in-payload total exists), and
stores it for approval. Adapters are DATA, so new utility/inverter coverage ships
without a new extension release.

Additive + self-contained: imports only stdlib + fastapi and owns its own sqlite
table (api/auto_adapters.db, override with env AUTO_ADAPTERS_DB). Mounted endpoints:
  GET  /v1/adapters/lookup?fp=<fingerprint>  -> approved adapter spec (404 none / 409 unapproved)
  POST /v1/adapters/ingest   {capture, fmt}  -> fingerprint, serve-or-synth, store candidate
  POST /v1/adapters/approve  {fingerprint}   -> (admin) promote candidate -> approved
  GET  /v1/adapters                          -> (admin) list registry
Admin endpoints require header X-Admin-Key == env AUTO_ADAPTERS_ADMIN_KEY when that
env var is set (open in dev when unset). The live agent tier shells to a local
`claude` binary if present; otherwise synthesis falls back to the heuristic only.
"""
import datetime as dt
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Header, HTTPException, Request

log = logging.getLogger(__name__)


# ============ generic interpreter (DATA-driven extraction) ============
def _strip_ns(root):
    for e in root.iter():
        if isinstance(e.tag, str) and "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return root


def _get_dot(obj, path):
    cur = obj
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def _json_records(root, steps):
    nodes = [root]
    for st in steps:
        nxt = []
        for n in nodes:
            coll = n
            for seg in st["path"].split("."):
                coll = coll.get(seg) if isinstance(coll, dict) else None
            if coll is None:
                continue
            coll = [coll] if isinstance(coll, dict) else coll
            for item in coll:
                w = st.get("where")
                if w and not all(str(item.get(k)) == str(v) for k, v in w.items()):
                    continue
                nxt.append(item)
        nodes = nxt
    return nodes


def _xml_records(root, steps):
    nodes = [root]
    for st in steps:
        nxt = []
        for n in nodes:
            for el in n.iter(st["path"]):
                w = st.get("where")
                if w and any((el.find(k) is None or (el.find(k).text or "").strip() != str(v)) for k, v in w.items()):
                    continue
                nxt.append(el)
        nodes = nxt
    return nodes


def _parse_date(v, kind):
    if v is None:
        return None
    try:
        if kind == "dotnet":  # "/Date(1781542800000)/"
            m = re.search(r"(\d{10,})", str(v))
            return dt.datetime.utcfromtimestamp(int(m.group(1)) / 1000).date().isoformat() if m else None
        if kind == "epoch_ms":
            return dt.datetime.utcfromtimestamp(int(v) / 1000).date().isoformat()
        if kind == "epoch_s":
            return dt.datetime.utcfromtimestamp(int(v)).date().isoformat()
        s = str(v).strip().split()[0]  # drop trailing time-of-day
        if kind == "my":
            m, y = s.split("/")
            return "%s-%02d" % (y, int(m))
        if kind == "mdy":
            m, d, y = s.split("/")
            return "%s-%02d-%02d" % (y, int(m), int(d))
        return s[:10]
    except Exception:
        return None


def _fval(rec, path, is_xml):
    if is_xml:
        node = rec if path == "." else rec.find(path)
        return node.text if node is not None else None
    return _get_dot(rec, path)


def extract(spec, raw):
    """Run a declarative spec against a raw capture -> (records, computed_kwh, summary_kwh)."""
    is_xml = spec["format"] == "xml"
    root = _strip_ns(ET.fromstring(raw)) if is_xml else (raw if isinstance(raw, (dict, list)) else json.loads(raw))
    recs = (_xml_records if is_xml else _json_records)(root, spec["records"])
    fd = spec["fields"]
    out = []
    for r in recs:
        g = _fval(r, fd["generation_kwh"]["path"], is_xml)
        if g is None:
            continue
        out.append({"date": _parse_date(_fval(r, fd["date"]["path"], is_xml), fd["date"].get("parse", "iso")),
                    "generation_kwh": round(float(g) * float(fd["generation_kwh"].get("scale", 1)), 3)})
    computed = round(sum(x["generation_kwh"] for x in out), 3)
    summary = None
    st = spec.get("summary_total_kwh")
    if st:
        if is_xml:
            node = root.find(".//" + st["path"])
            sv = node.text if node is not None else None
        else:
            sv = _get_dot(root, st["path"])
        if sv is not None:
            summary = round(float(sv) * float(st.get("scale", 1)), 3)
    return out, computed, summary


def validate(recs, computed, summary):
    """Reconcile when an independent total exists; else structural pass (delta None)."""
    hard, notes = [], []
    if not recs:
        hard.append("no records")
    if any(x["generation_kwh"] < 0 or x["generation_kwh"] > 100000 for x in recs):
        hard.append("implausible value")
    dates = [x["date"] for x in recs]
    if any(d is None for d in dates):
        hard.append("unparseable date")
    if len(set(dates)) != len(dates):
        notes.append("duplicate dates (multi-site snapshot or possible double-count)")
    delta = None
    if summary is not None:
        delta = abs(computed - summary) / summary if summary else 1.0
        if delta > 0.02:
            hard.append("reconcile mismatch %.1f%%" % (delta * 100))
    return len(hard) == 0, hard + notes, delta


def fingerprint(raw, fmt):
    """Coarse PLATFORM fingerprint (same platform -> same fingerprint -> reuse adapter)."""
    if fmt == "xml":
        root = ET.fromstring(raw)
        tag = root.tag
        ns = tag[1:tag.index("}")] if "}" in tag else ""
        return "xml:%s:%s" % (tag.split("}")[-1], ns)
    obj = raw if isinstance(raw, (dict, list)) else json.loads(raw)
    keys = ",".join(sorted(obj.keys())) if isinstance(obj, dict) else "list"
    return "json:" + keys


# ============ synthesis: cheap heuristic -> Claude agent ============
GEN = ["export", "received", "generated", "generation", "production", "produced", "banked", "surplus"]
NEG = ["import", "delivered", "used", "consum", "demand", "peak", "cost", "bill", "net"]
DATEK = ["date", "ts", "time", "month", "read", "start", "period", "day", "import"]
TOTAL = ["total", "ytd", "period", "sum", "overall"]

SCHEMA_DOC = """The interpreter understands EXACTLY this spec schema:
{ "format":"json"|"xml",
  "records":[{"path":"<key>","where":{"<field>":"<value>"}(optional)}, ...],
  "fields":{"date":{"path":"<key>","parse":"iso|epoch_ms|epoch_s|my|mdy|dotnet"},
            "generation_kwh":{"path":"<key>","scale":<number that converts the source value to kWh>}},
  "summary_total_kwh":{"path":"<dotted path from root>","scale":<number>} (OPTIONAL - omit if no independent total exists) }
JSON: each records step indexes into the array/object at path (dotted ok); where keeps items whose field==value; field path is an object key; summary path is dotted from root.
XML: each records step is a descendant TAG name; where matches a child element text; a field path is a child tag (or "." for the element's own text); summary path is a descendant tag."""


def _lists(obj, path=""):
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            np = (path + "." + k) if path else k
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out.append((np, v))
            out += _lists(v, np)
    return out


def heuristic(raw):
    obj = raw if isinstance(raw, (dict, list)) else json.loads(raw)
    rec_path = gen_f = sample = None
    for path, lst in _lists(obj):
        item = lst[0]
        g = next((k for k in item if isinstance(item[k], (int, float))
                  and any(s in k.lower() for s in GEN) and not any(s in k.lower() for s in NEG)), None)
        if g:
            rec_path, gen_f, sample = path, g, lst
            break
    if not gen_f:
        return None
    date_f = next((k for k in sample[0] if any(s in k.lower() for s in DATEK)), None)
    if not date_f:
        return None
    dv = str(sample[0][date_f])
    parse = ("epoch_ms" if dv.isdigit() and len(dv) >= 13 else "epoch_s" if dv.isdigit()
             else "my" if dv.count("/") == 1 else "mdy" if "/" in dv else "iso")
    vals = [it[gen_f] for it in sample if isinstance(it.get(gen_f), (int, float))]
    scale = 0.001 if (vals and sorted(vals)[len(vals) // 2] > 5000) else 1

    def find_total(o, p=""):
        if isinstance(o, dict):
            for k, v in o.items():
                np = (p + "." + k) if p else k
                if isinstance(v, (int, float)) and any(t in k.lower() for t in TOTAL) and any(gg in k.lower() for gg in GEN):
                    return np
                r = find_total(v, np)
                if r:
                    return r
        return None
    total = find_total(obj)
    spec = {"format": "json", "records": [{"path": rec_path}],
            "fields": {"date": {"path": date_f, "parse": parse}, "generation_kwh": {"path": gen_f, "scale": scale}}}
    if total:
        spec["summary_total_kwh"] = {"path": total, "scale": 1}
    return spec


def _find_claude():
    for c in [os.environ.get("CLAUDE_BIN"), shutil.which("claude"),
              "/root/.hermes/node/bin/claude", "/root/.local/bin/claude",
              os.path.expanduser("~/.local/bin/claude")]:
        if c and os.path.exists(c):
            return c
    return None


def agent(raw_text, fmt, feedback=None):
    cb = _find_claude()
    if not cb:
        return None, "agent (unavailable)"
    prompt = ("You are an adapter synthesizer. Below is a raw %s response captured from a solar customer's utility/inverter "
              "portal you have never seen. Emit a declarative extraction SPEC (JSON) that pulls, for each period or site, the "
              "SOLAR GENERATION produced/exported/received (NOT imported / consumed / used / net / demand), plus an independent "
              "reconciliation total IF one exists in the payload.\n\n%s\n\nRules: choose the generation field, not consumption. "
              "Set scale so generation is in kWh (already kWh -> 1, Wh -> 0.001). Only include summary_total_kwh if the payload "
              "truly contains an independent total. Return ONLY the JSON spec, no prose, no fences.%s\n\nPAYLOAD:\n%s") % (
              fmt, SCHEMA_DOC, ("\n\nYour previous attempt FAILED validation: " + feedback if feedback else ""), raw_text)
    try:
        p = subprocess.run([cb, "-p", "--output-format", "text"], input=prompt,
                           capture_output=True, text=True, timeout=240)
        m = re.search(r"\{.*\}", p.stdout, re.S)
        if m:
            return json.loads(m.group(0)), "agent (live)"
        log.warning("auto_adapters.agent: no JSON object in agent output (fmt=%s)", fmt)
    except json.JSONDecodeError as e:
        # The agent returned text that looked like JSON but didn't parse — log the
        # reason instead of silently falling through, so a recurring bad-output
        # pattern is visible. Control flow unchanged: caller retries / falls back.
        log.warning("auto_adapters.agent: malformed JSON spec from agent (fmt=%s): %s", fmt, e)
    except Exception as e:
        log.warning("auto_adapters.agent: synthesis subprocess failed (fmt=%s): %s", fmt, e)
    return None, "agent (error)"


def synthesize(raw_text, fmt):
    """Returns (spec, source, reconcile_delta_or_None, ok)."""
    if fmt == "json":
        spec = heuristic(raw_text)
        if spec:
            try:
                recs, c, s = extract(spec, raw_text)
                ok, _, d = validate(recs, c, s)
                if ok:
                    return spec, "heuristic", d, True
            except Exception as e:
                # Heuristic spec didn't apply to this payload — expected fallback
                # to the agent tier, but log so a systematically-broken heuristic
                # is diagnosable instead of invisible.
                log.debug("auto_adapters.synthesize: heuristic extract failed, "
                          "falling back to agent (fmt=%s): %s", fmt, e)
    feedback = None
    for _ in range(2):  # synth -> validate -> repair loop
        spec, src = agent(raw_text, fmt, feedback)
        if not spec:
            break
        try:
            recs, c, s = extract(spec, raw_text)
        except Exception as e:
            feedback = "extract error: %s" % e
            continue
        ok, reasons, d = validate(recs, c, s)
        if ok:
            return spec, src, d, True
        feedback = "%s (computed=%s reconcile_total=%s)" % (reasons, c, s)
    return None, "none", None, False


# ============ registry (sqlite) ============
_DB = os.environ.get("AUTO_ADAPTERS_DB",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_adapters.db"))


def _conn():
    c = sqlite3.connect(_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS auto_adapters(
        fingerprint TEXT PRIMARY KEY, fmt TEXT, spec TEXT, status TEXT,
        reconcile REAL, source TEXT, version INTEGER, created TEXT, updated TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS auto_readings(
        source TEXT, fingerprint TEXT, date TEXT, kwh REAL, captured_at TEXT)""")
    return c


def reg_get(fp):
    c = _conn()
    r = c.execute("SELECT fingerprint,fmt,spec,status,reconcile,source,version FROM auto_adapters WHERE fingerprint=?",
                  (fp,)).fetchone()
    c.close()
    if not r:
        return None
    return {"fingerprint": r[0], "fmt": r[1], "spec": r[2], "status": r[3],
            "reconcile": r[4], "source": r[5], "version": r[6]}


def reg_upsert(fp, fmt, spec, reconcile, source):
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    c = _conn()
    cur = reg_get(fp)
    ver = (cur["version"] + 1) if cur else 1
    c.execute("""INSERT INTO auto_adapters(fingerprint,fmt,spec,status,reconcile,source,version,created,updated)
                 VALUES(?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(fingerprint) DO UPDATE SET fmt=?,spec=?,status='candidate',reconcile=?,source=?,version=?,updated=?""",
              (fp, fmt, json.dumps(spec), "candidate", reconcile, source, ver, now, now,
               fmt, json.dumps(spec), reconcile, source, ver, now))
    c.commit()
    c.close()
    return ver


def reg_approve(fp):
    c = _conn()
    c.execute("UPDATE auto_adapters SET status='approved', updated=? WHERE fingerprint=?",
              (dt.datetime.utcnow().isoformat(timespec="seconds"), fp))
    n = c.total_changes
    c.commit()
    c.close()
    return n


def reg_all():
    c = _conn()
    rows = c.execute("SELECT fingerprint,fmt,status,reconcile,source,version,updated FROM auto_adapters ORDER BY updated DESC").fetchall()
    c.close()
    return [{"fingerprint": r[0], "fmt": r[1], "status": r[2], "reconcile": r[3],
             "source": r[4], "version": r[5], "updated": r[6]} for r in rows]


# ============ FastAPI router ============
router = APIRouter()


def _require_admin(key):
    expected = os.environ.get("AUTO_ADAPTERS_ADMIN_KEY")
    if not expected or key != expected:  # deny-by-default: closed unless a key is configured AND matches
        raise HTTPException(status_code=403, detail="admin disabled (set AUTO_ADAPTERS_ADMIN_KEY)")


def _auth_tenant(authorization):
    """Gate the tenant-facing endpoints. In the full app, validate the bearer via the
    app's tenant_from_bearer (lazy import avoids a circular import at module load). In a
    standalone/test context where that import isn't available, require a bearer present."""
    try:
        from .app import tenant_from_bearer
    except Exception:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="authorization required")
        return None
    return tenant_from_bearer(authorization)


@router.get("/v1/adapters/lookup")
def adapters_lookup(fp: str, authorization: str = Header(default=None)):
    _auth_tenant(authorization)
    r = reg_get(fp)
    if r and r["status"] == "approved":
        return json.loads(r["spec"])
    if r:
        raise HTTPException(status_code=409, detail="adapter exists but awaiting approval")
    raise HTTPException(status_code=404, detail="no adapter for this platform yet")


@router.post("/v1/adapters/ingest")
async def adapters_ingest(request: Request, authorization: str = Header(default=None)):
    _auth_tenant(authorization)
    body = await request.json()
    cap = body.get("capture")
    fmt = body.get("fmt", "json")
    if cap is None:
        raise HTTPException(status_code=400, detail="missing 'capture'")
    raw_text = cap if isinstance(cap, str) else json.dumps(cap)
    try:
        fp = fingerprint(raw_text, fmt)
    except Exception as e:
        raise HTTPException(status_code=400, detail="cannot fingerprint capture: %s" % e)

    existing = reg_get(fp)
    if existing:
        try:  # health-probe the cached adapter against this fresh capture
            recs, c, s = extract(json.loads(existing["spec"]), raw_text)
            ok, _reasons, d = validate(recs, c, s)
        except Exception as e:
            # Cached adapter failed to apply (portal changed, or a corrupt stored
            # spec) — log it; flow continues to the auto-repair synth below.
            log.info("auto_adapters.ingest: cached adapter probe failed for fp=%s, "
                     "auto-repairing: %s", fp, e)
            ok, d = False, None
        if ok:
            return {"fingerprint": fp, "status": existing["status"], "result": "cache_hit", "reconcile": d}
        spec, src, d, okk = synthesize(raw_text, fmt)  # adapter broke -> auto-repair
        if okk:
            ver = reg_upsert(fp, fmt, spec, d, src)
            return {"fingerprint": fp, "status": "candidate", "result": "repaired",
                    "source": src, "reconcile": d, "version": ver}
        return {"fingerprint": fp, "status": existing["status"], "result": "degraded_repair_failed"}

    spec, src, d, ok = synthesize(raw_text, fmt)
    if not ok:
        return {"fingerprint": fp, "status": "none", "result": "synth_failed", "source": src}
    ver = reg_upsert(fp, fmt, spec, d, src)
    return {"fingerprint": fp, "status": "candidate", "result": "synthesized", "source": src,
            "reconcile": d, "level": ("reconciled" if d is not None else "structural"), "version": ver}


@router.post("/v1/adapters/approve")
async def adapters_approve(request: Request, x_admin_key: str = Header(default=None)):
    _require_admin(x_admin_key)
    body = await request.json()
    fp = body.get("fingerprint")
    if not fp:
        raise HTTPException(status_code=400, detail="missing 'fingerprint'")
    return {"approved": reg_approve(fp), "fingerprint": fp}


@router.get("/v1/adapters")
def adapters_list(x_admin_key: str = Header(default=None)):
    _require_admin(x_admin_key)
    return {"adapters": reg_all()}


@router.post("/v1/adapters/readings")
async def adapters_readings(request: Request, authorization: str = Header(default=None)):
    _auth_tenant(authorization)
    """Sink for normalized generation the extension extracted via a served adapter."""
    body = await request.json()
    source = body.get("source", "?")
    fp = body.get("fingerprint", "?")
    recs = body.get("records", [])
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    c = _conn()
    for r in recs:
        c.execute("INSERT INTO auto_readings VALUES(?,?,?,?,?)",
                  (source, fp, r.get("date"), r.get("generation_kwh"), now))
    c.commit()
    c.close()
    return {"stored": len(recs), "source": source, "fingerprint": fp}


@router.get("/v1/adapters/fleet")
def adapters_fleet(x_admin_key: str = Header(default=None)):
    _require_admin(x_admin_key)
    c = _conn()
    sites = c.execute("""SELECT source, COUNT(*), ROUND(SUM(kwh),1), MIN(date), MAX(date)
                         FROM auto_readings GROUP BY source ORDER BY SUM(kwh) DESC""").fetchall()
    total = c.execute("SELECT ROUND(SUM(kwh),1), COUNT(DISTINCT source) FROM auto_readings").fetchone()
    c.close()
    return {"sites": [{"source": s[0], "readings": s[1], "kwh": s[2], "from": s[3], "to": s[4]} for s in sites],
            "total_kwh": total[0], "site_count": total[1]}
