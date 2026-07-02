#!/usr/bin/env python3
"""Discover + verify SmartHub hosts for the provider catalog (fleet coverage sweep).

Two jobs, both grounded on NISC's own unauthenticated branding endpoint
``GET https://<host>/services/member/siteName`` which returns
``Loading <Utility Name> SmartHub Application``:

  AUDIT     every row that already has a smarthub_host — does the host's
            siteName actually match the row's label? Catches mis-pairings
            like bartonelectric.smarthub.coop (Barton County Electric, MO)
            sitting on VT's "Village of Barton" row: the exact misattribution
            bug class the sh_* discovery code warns about.

  DISCOVER  every row WITHOUT a host — generate candidate subdomains from the
            label/code (patterns mirrored from the 471 known hosts), DNS-probe
            them, fetch siteName for the ones that resolve, and classify:
              CONFIRMED  strong name match  → safe to wire (--apply writes CSV)
              REVIEW     resolves + partial match → human/agent review file
              (no match / no DNS → silently dropped)

Name matching is deliberately conservative: distinctive-token containment or
an exact acronym hit. Geographic qualifiers (county/village/city/town) count
as distinctive so "Barton County" can never confirm "Village of Barton".

Usage:
    python3 scripts/discover_smarthub_hosts.py             # report only
    python3 scripts/discover_smarthub_hosts.py --apply     # + write CSVs
    python3 scripts/discover_smarthub_hosts.py --audit-only

Output: scripts/out/smarthub_discovery.json (full report, incl. REVIEW list).
Network: DNS lookups + one small GET per resolving candidate. ~64-way DNS,
8-way HTTPS, 8s timeouts — polite to NISC's infra.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import glob
import json
import pathlib
import re
import socket
import ssl
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV_GLOB = str(ROOT / "api" / "data" / "providers" / "*.csv")
OUT_DIR = ROOT / "scripts" / "out"
SUFFIX = ".smarthub.coop"

# Tokens too generic to identify a utility on their own. Geographic qualifiers
# (county/village/city/town/district) are NOT here — they distinguish.
GENERIC = {
    "electric", "electrical", "cooperative", "coop", "co", "op", "corp",
    "corporation", "inc", "incorporated", "association", "assn", "power",
    "energy", "utility", "utilities", "services", "service", "membership",
    "company", "of", "the", "and", "rural", "public", "board", "works",
    "light", "lighting", "department", "dept", "municipal", "authority",
    "system", "systems", "telephone", "member", "members",
    # SmartHub UI / product noise seen in real siteName responses
    "smarthub", "web", "ebill", "e", "bill", "account", "management",
    "portal", "application", "login", "my", "fiber", "broadband",
}

# siteName responses that identify the PRODUCT, not the utility — inconclusive.
GENERIC_SITENAMES = {"smarthub", "electric cooperative smarthub", "smart hub"}


def norm_tokens(s: str) -> list[str]:
    toks = [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]
    # SmartHub brand styling glues a "my" prefix onto the utility token
    # (MyDEMCO, myMTE, MyNOVEC). Split it off so the real identity token matches
    # — the prefix alone is meaningless and already in GENERIC.
    out = []
    for t in toks:
        m = re.match(r"^my([a-z].{2,})$", t)
        out.append(m.group(1) if m else t)
    return out


def distinctive(tokens: list[str]) -> set[str]:
    return {t for t in tokens if t not in GENERIC}


def condensed(tokens: list[str]) -> str:
    return "".join(tokens)


def acronym(tokens: list[str]) -> str:
    return "".join(t[0] for t in tokens if t not in {"of", "the", "and"})


def acronym_prefixes(tokens: list[str]) -> set[str]:
    """All prefix acronyms of the token list (skipping of/the/and), len>=3.
    'Washington Electric Co-op' -> {wec, weco}; 'Middle Tennessee Electric
    Membership Corp' -> {mte, mtem, mtemc} — so brand acronyms like WEC/myMTE
    confirm without loosening anything else."""
    ts = [t for t in tokens if t not in {"of", "the", "and"}]
    return {"".join(t[0] for t in ts[:k]) for k in range(3, len(ts) + 1)}


def strip_sitename(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^Loading\s+", "", s)
    s = re.sub(r"\s+SmartHub Application$", "", s, flags=re.I)
    s = re.sub(r"\s+Application$", "", s, flags=re.I)
    return s.strip()


def match_names(label: str, sitename: str) -> str:
    """'confirmed' | 'review' | 'reject' | 'generic' — conservative by design.
    False CONFIRMED = misattribution (the bug class sh_* discovery exists to
    prevent), so every widening here is a narrow, exact-form rule."""
    lt, st = norm_tokens(label), norm_tokens(sitename)
    if not st:
        return "reject"
    if " ".join(st) in GENERIC_SITENAMES:
        return "generic"                      # product-branded, tells us nothing
    ld, sd = distinctive(lt), distinctive(st)
    if not ld or not sd:
        return "review" if (set(lt) & set(st)) else "reject"
    # exact normalized equality
    if lt == st:
        return "confirmed"
    # brand-acronym hit: the sitename's one distinctive token IS an acronym of
    # the label (WEC, myMTE->mte, DMEA) — exact prefix-acronym match only.
    if len(sd) == 1:
        tok = next(iter(sd))
        if len(tok) >= 3 and tok in acronym_prefixes(lt):
            return "confirmed"
    if len(ld) == 1:
        tok = next(iter(ld))
        if len(tok) >= 3 and tok in acronym_prefixes(st):
            return "confirmed"
    # geographic qualifiers conflict -> can never confirm (Barton County vs
    # Village of Barton)
    quals = {"county", "village", "city", "town", "parish", "district"}
    lq, sq = set(lt) & quals, set(st) & quals
    conflict = bool(lq and sq and lq != sq)
    # condensed containment (brand styles: MyBluebonnet, BEMC.SmartHub,
    # 'Cookson HillsElectric', 'Ark Valley'): distinctive-condensed of one side
    # inside the other's full-condensed, min length 5.
    lc, sc = condensed(lt), condensed(st)
    ldc = condensed([t for t in lt if t in ld])   # order-preserving
    sdc = condensed([t for t in st if t in sd])
    if not conflict:
        for needle, hay in ((sdc, lc), (ldc, sc)):
            if len(needle) >= 5 and needle in hay:
                return "confirmed"
    # containment of distinctive token sets, either direction
    if ld <= sd or sd <= ld:
        return "review" if conflict else "confirmed"
    if ld & sd:
        return "review"
    return "reject"


# Suffix stems seen across the 496 known NISC hosts (electric/ec/emc/rec/…) plus
# municipal/PUD forms (pud/mud/bpw/ppd/blp/…). Applied to condensed name stems.
_SUFFIXES = [
    "electric", "ec", "emc", "rec", "recc", "reca", "remc", "cea", "ce", "elec",
    "ecc", "eci", "epa", "coop", "cooperative", "energy", "power", "light",
    "lightandpower", "utilities", "utility", "pud", "mud", "bpw", "ppd", "ud",
    "mu", "blp", "el", "e",
]
_QUALIFIERS = {"county", "village", "city", "town", "parish", "district",
               "borough", "township", "of"}


def candidates(code: str, label: str, taken: set[str], state: str = "") -> list[str]:
    """Candidate subdomains, mirroring patterns seen across the known hosts. Round-2:
    condensed stems (full / distinctive / distinctive-minus-geographic-qualifier),
    each × the full suffix set; first-token × suffixes; hyphenated first-two;
    acronym × suffixes; state-abbrev prefix; my+name. Priority-ordered, cap 36."""
    lt = norm_tokens(label)
    ld = [t for t in lt if t not in GENERIC]
    ld_nq = [t for t in ld if t not in _QUALIFIERS]     # drop county/village/… noise
    acr = acronym(lt)
    st = (state or "").strip().lower()
    outs: list[str] = []

    def add(x: str | None):
        x = re.sub(r"[^a-z0-9-]", "", (x or "").lower())
        if 2 <= len(x) <= 40 and x not in outs:
            outs.append(x)

    add(code)
    # condensed-name stems, each with every suffix + a state-prefixed form
    for stem in dict.fromkeys(["".join(ld_nq), "".join(ld), "".join(lt)]):
        if not stem:
            continue
        add(stem)
        for suf in _SUFFIXES:
            add(stem + suf)
        if st:
            add(st + stem)
    # first distinctive token alone + suffixes; hyphenated / joined first-two
    if ld_nq:
        add(ld_nq[0])
        for suf in _SUFFIXES:
            add(ld_nq[0] + suf)
        if len(ld_nq) >= 2:
            add(ld_nq[0] + "-" + ld_nq[1])
            add(ld_nq[0] + ld_nq[1])
    # acronym forms (WEC, DMEA, …)
    if len(acr) >= 3:
        add(acr)
        for suf in ("ec", "emc", "rec", "coop", "energy", "power"):
            add(acr + suf)
    add("my" + "".join(ld_nq))
    return [c for c in outs if (c + SUFFIX) not in taken][:36]


_SH_RE = re.compile(r"https?://([a-z0-9][a-z0-9-]*\.smarthub\.coop)", re.I)


def _fetch_page(url: str, timeout: int = 8) -> tuple[str | None, str]:
    """Fetch a co-op's own site (follow redirects). Returns (final_url, body[:200k])."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 EnergyAgent-catalog/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            body = r.read(200_000).decode("utf-8", "replace")
            return r.geturl(), body
    except Exception:
        return None, ""


def portal_hosts(rows: list[dict]) -> list[tuple[dict, str]]:
    """High-yield mode: for each hostless row with a non-smarthub portal_url, fetch
    the co-op's site and harvest any *.smarthub.coop host it links to or redirects
    to (co-op homepages routinely deep-link their SmartHub billing portal). These
    still flow through the same siteName-verify + conservative name-match, so a
    harvested host is only wired if its branding confirms the utility."""
    def probe(r: dict) -> tuple[dict, list[str]] | None:
        url = (r.get("portal_url") or "").strip()
        if not url or "smarthub.coop" in url.lower():
            return None
        final, body = _fetch_page(url)
        hosts: set[str] = set()
        if final:
            m = _SH_RE.search(final)
            if m:
                hosts.add(m.group(1).lower())
        for m in _SH_RE.finditer(body):
            hosts.add(m.group(1).lower())
        return (r, sorted(hosts)) if hosts else None

    out: list[tuple[dict, str]] = []
    probe_rows = [r for r in rows if (r.get("portal_url") or "").strip()]
    with cf.ThreadPoolExecutor(24) as ex:
        for res in ex.map(probe, probe_rows):
            if res:
                r, hosts = res
                for h in hosts:
                    out.append((r, h))
    return out


def dns_ok(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


_CTX = ssl.create_default_context()


def fetch_sitename(host: str) -> str | None:
    url = f"https://{host}/services/member/siteName"
    req = urllib.request.Request(url, headers={"User-Agent": "EnergyAgent-catalog-verify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8, context=_CTX) as r:
            if r.status != 200:
                return None
            body = r.read(500).decode("utf-8", "replace").strip()
            # a real deployment answers with a short plain-text name, not HTML
            if not body or body.startswith("<"):
                return None
            return strip_sitename(body)
    except Exception:
        return None


def load_rows() -> list[dict]:
    rows = []
    for f in sorted(glob.glob(CSV_GLOB)):
        with open(f, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                row["_file"] = f
                rows.append(row)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write confirmed hosts into the CSVs")
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap hostless rows probed (debug)")
    args = ap.parse_args()

    rows = load_rows()
    hosted = [r for r in rows if (r.get("smarthub_host") or "").strip()]
    hostless = [r for r in rows if not (r.get("smarthub_host") or "").strip()]
    taken = {(r.get("smarthub_host") or "").strip().lower() for r in hosted}
    print(f"catalog: {len(rows)} rows — {len(hosted)} hosted, {len(hostless)} hostless")

    report: dict = {"audit": [], "reassigned": [], "confirmed": [], "review": [], "stats": {}}

    # A sitename that CONFIRMS exactly one catalog row identifies that host's
    # true owner — used to reassign mis-paired hosts and harvest cross-matches.
    def owner_rows(sitename: str, rows_pool: list[dict]) -> list[dict]:
        return [r for r in rows_pool if match_names(r["label"], sitename) == "confirmed"]

    # ── AUDIT ────────────────────────────────────────────────────────────────
    print("auditing existing hosts…")
    with cf.ThreadPoolExecutor(8) as ex:
        names = list(ex.map(lambda r: fetch_sitename(r["smarthub_host"].strip()), hosted))
    n_generic = 0
    for r, name in zip(hosted, names):
        verdict = "dead" if name is None else match_names(r["label"], name)
        if verdict == "generic":
            n_generic += 1        # unbranded instance — inconclusive, leave as-is
            continue
        if verdict != "confirmed":
            entry = {
                "code": r["code"], "label": r["label"], "state": r.get("state", ""),
                "host": r["smarthub_host"].strip(), "sitename": name, "verdict": verdict,
                "file": r["_file"],
            }
            # Mis-paired host: does its sitename confirm exactly ONE hostless row?
            # Then we know the true owner — move the host there, clear it here.
            if verdict == "reject" and name:
                owners = owner_rows(name, hostless)
                if len(owners) == 1:
                    o = owners[0]
                    entry["reassign_to"] = {"code": o["code"], "label": o["label"],
                                            "state": o.get("state", ""), "file": o["_file"]}
                    report["reassigned"].append(entry)
                    continue
            report["audit"].append(entry)
    ok = len(hosted) - len(report["audit"]) - len(report["reassigned"]) - n_generic
    print(f"  audit: {ok}/{len(hosted)} name-confirmed, {n_generic} generic (left as-is), "
          f"{len(report['reassigned'])} mis-paired->reassignable, {len(report['audit'])} flagged")

    if not args.audit_only:
        # ── DISCOVER ─────────────────────────────────────────────────────────
        probe_rows = hostless[: args.limit] if args.limit else hostless
        cand_map: list[tuple[dict, str]] = []
        for r in probe_rows:
            for c in candidates(r["code"], r["label"], taken, r.get("state", "")):
                cand_map.append((r, c + SUFFIX))
        print(f"probing DNS for {len(cand_map)} candidate hosts across {len(probe_rows)} utilities…")
        with cf.ThreadPoolExecutor(64) as ex:
            alive = list(ex.map(lambda rc: dns_ok(rc[1]), cand_map))
        live = [rc for rc, a in zip(cand_map, alive) if a]
        print(f"  {len(live)} candidate hosts resolve")
        # HIGH-YIELD: harvest hosts co-ops link from their OWN site (real links, no
        # guessing). Merge into the verify set (dedup by host); they skip DNS.
        harvested = portal_hosts(probe_rows)
        seen_hosts = {h for _, h in live}
        n_new_portal = 0
        for r, h in harvested:
            if h not in taken and h not in seen_hosts:
                live.append((r, h))
                seen_hosts.add(h)
                n_new_portal += 1
        report["stats"]["portal_hosts_harvested"] = n_new_portal
        print(f"  portal-scrape harvested {n_new_portal} new smarthub host(s) from co-op sites")
        print(f"  {len(live)} total to verify — fetching siteName…")
        with cf.ThreadPoolExecutor(8) as ex:
            sn = list(ex.map(lambda rc: fetch_sitename(rc[1]), live))

        # best classification per row; a host can only be claimed once.
        # CROSS-MATCH: a candidate guessed for row A whose sitename actually
        # confirms row B is a find for B — every verified (host, sitename) pair
        # is matched against EVERY hostless row, not just the row that guessed it.
        claimed: set[str] = set(taken)
        pairs: dict[str, str] = {}                     # host -> sitename (verified, unclaimed)
        for (r, host), name in zip(live, sn):
            if name and host not in claimed:
                pairs.setdefault(host, name)
        by_row: dict[str, dict] = {}
        for host, name in pairs.items():
            if " ".join(norm_tokens(name)) in GENERIC_SITENAMES:
                continue                               # unbranded — cannot verify ownership
            owners = owner_rows(name, hostless)
            if len(owners) == 1:
                r, verdict = owners[0], "confirmed"
            elif len(owners) > 1:
                continue                               # ambiguous (same-name utilities in 2 states)
            else:
                # no confirmed owner — keep a review entry for the row whose
                # guess found it, if the names at least overlap
                cands = [rr for rr in hostless if match_names(rr["label"], name) == "review"]
                if len(cands) != 1:
                    continue
                r, verdict = cands[0], "review"
            cur = by_row.get(r["code"])
            rank = {"confirmed": 2, "review": 1}[verdict]
            if cur and cur["_rank"] >= rank:
                continue
            by_row[r["code"]] = {
                "code": r["code"], "label": r["label"], "state": r.get("state", ""),
                "host": host, "sitename": name, "verdict": verdict,
                "file": r["_file"], "_rank": rank,
            }
        for v in by_row.values():
            v.pop("_rank")
            report[v["verdict"] == "confirmed" and "confirmed" or "review"].append(v)
        # one host must not confirm for two rows
        seen_hosts: dict[str, str] = {}
        deduped = []
        for v in sorted(report["confirmed"], key=lambda x: x["code"]):
            if v["host"] in seen_hosts:
                v["verdict"] = "review"
                v["conflict_with"] = seen_hosts[v["host"]]
                report["review"].append(v)
            else:
                seen_hosts[v["host"]] = v["code"]
                deduped.append(v)
        report["confirmed"] = deduped
        print(f"  discovery: {len(report['confirmed'])} CONFIRMED, {len(report['review'])} for review")

        # ── APPLY ────────────────────────────────────────────────────────────
        if args.apply and (report["confirmed"] or report["reassigned"]):
            # per-file edit plan: sets (wire a host) and clears (remove a mis-paired one)
            sets: dict[str, dict[str, dict]] = {}
            clears: dict[str, dict[str, dict]] = {}
            for v in report["confirmed"]:
                sets.setdefault(v["file"], {})[v["code"]] = {
                    "host": v["host"], "sitename": v["sitename"]}
            for v in report["reassigned"]:
                clears.setdefault(v["file"], {})[v["code"]] = v
                tgt = v["reassign_to"]
                sets.setdefault(tgt["file"], {})[tgt["code"]] = {
                    "host": v["host"], "sitename": v["sitename"]}
            n_set = n_clear = 0
            for f in sorted(set(sets) | set(clears)):
                with open(f, encoding="utf-8", newline="") as fh:
                    rd = csv.DictReader(fh)
                    fields = rd.fieldnames
                    frows = list(rd)
                for row in frows:
                    c = clears.get(f, {}).get(row["code"])
                    if c:
                        row["smarthub_host"] = ""
                        row["portal_url"] = ""
                        row["scrape_status"] = "in-progress"
                        note = (row.get("notes") or "").strip()
                        row["notes"] = (note + f" MIS-PAIRED host {c['host']} removed Jul 2026 — "
                                        f"siteName says it belongs to '{c['sitename']}' "
                                        f"({c['reassign_to']['code']}).").strip()
                        n_clear += 1
                    s = sets.get(f, {}).get(row["code"])
                    if s:
                        row["smarthub_host"] = s["host"]
                        row["portal_url"] = f"https://{s['host']}"
                        row["scrape_status"] = "live"
                        note = (row.get("notes") or "").strip()
                        stamp = f"SmartHub host auto-discovered + siteName-verified ('{s['sitename']}') Jul 2026."
                        row["notes"] = (note + " " + stamp).strip()
                        n_set += 1
                with open(f, "w", encoding="utf-8", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=fields)
                    w.writeheader()
                    for row in frows:
                        row.pop("_file", None)
                        w.writerow(row)
            print(f"  applied: {n_set} hosts wired, {n_clear} mis-pairings cleared")

    report["stats"] = {
        "rows": len(rows), "hosted_before": len(hosted),
        "audit_flagged": len(report["audit"]),
        "audit_reassigned": len(report["reassigned"]),
        "discovered_confirmed": len(report["confirmed"]),
        "discovered_review": len(report["review"]),
    }
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "smarthub_discovery.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
