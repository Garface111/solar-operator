#!/usr/bin/env python3
"""Harvest SmartHub hosts from co-op WEBSITES for hostless rows with no portal_url.

Round-4 extension of scripts/discover_smarthub_hosts.py. That script's portal-scrape
mode only reaches hostless rows that already carry a portal_url (471 rows); another
~630 hostless rows have no website on file at all. This tool closes that gap:

  1. For each hostless catalog row (no smarthub_host, no portal_url needed), GUESS
     likely co-op website domains from the label (condensed/acronym stems x
     .com/.coop/.org — the conventions real co-op sites follow: avecc.com,
     tcec.coop, boonepower.com, lcec.coop ...).
  2. DNS-probe the guesses, GET the ones that resolve (one polite fetch each),
     and harvest any *.smarthub.coop host the page links to.
  3. Feed every harvested host through the SAME gate as discovery: fetch its
     /services/member/siteName branding and wire it only if it CONFIRMS exactly
     one hostless catalog row (conservative match, exact-form rules).

Guessed WEBSITES are only an enumeration source — a wrong site can never cause a
wrong pairing, because ownership is decided solely by the host's own branding.
For the same reason we do NOT write portal_url from guesses; only verified
smarthub hosts are written.

Usage:
    python3 scripts/harvest_coop_site_hosts.py            # report only
    python3 scripts/harvest_coop_site_hosts.py --apply    # write confirmed hosts
    python3 scripts/harvest_coop_site_hosts.py --limit 20 # debug cap

Output: scripts/out/coop_site_harvest.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from discover_smarthub_hosts import (  # noqa: E402
    GENERIC, GENERIC_SITENAMES, OUT_DIR, _QUALIFIERS, _SH_RE, _fetch_page,
    acronym, dns_ok, fetch_sitename, load_rows, match_names, norm_tokens,
)

TLDS = [".com", ".coop", ".org", ".net"]


def site_guesses(label: str) -> list[str]:
    """Candidate co-op website domains (no scheme), priority-ordered, cap 14."""
    lt = norm_tokens(label)
    ld = [t for t in lt if t not in GENERIC]
    ld_nq = [t for t in ld if t not in _QUALIFIERS]
    acr = acronym(lt)
    stems: list[str] = []

    def add(stem: str | None):
        if stem and 2 <= len(stem) <= 30 and stem not in stems:
            stems.append(stem)

    add("".join(ld_nq))
    add("".join(ld))
    if len(acr) >= 3:
        add(acr)
    if ld_nq:
        for suf in ("electric", "ec", "emc", "power", "energy", "coop"):
            add(ld_nq[0] + suf)
    doms: list[str] = []
    for s in stems:
        for tld in TLDS:
            doms.append(s + tld)
    return doms[:14]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = load_rows()
    hostless = [r for r in rows if not (r.get("smarthub_host") or "").strip()]
    taken = {(r.get("smarthub_host") or "").strip().lower()
             for r in rows if (r.get("smarthub_host") or "").strip()}
    # rows worth guessing sites for: no portal_url on file (portal-scrape already
    # covers the ones that have one)
    targets = [r for r in hostless if not (r.get("portal_url") or "").strip()]
    if args.limit:
        targets = targets[: args.limit]
    print(f"catalog: {len(rows)} rows — {len(hostless)} hostless, "
          f"{len(targets)} with no site on file")

    cand: list[tuple[dict, str]] = []
    for r in targets:
        for d in site_guesses(r["label"]):
            cand.append((r, d))
    print(f"DNS-probing {len(cand)} guessed co-op domains…")
    with cf.ThreadPoolExecutor(64) as ex:
        alive = list(ex.map(lambda rc: dns_ok(rc[1]), cand))
    live = [rc for rc, a in zip(cand, alive) if a]
    print(f"  {len(live)} resolve — fetching (harvesting smarthub links)…")

    def harvest(rc: tuple[dict, str]) -> set[str]:
        _, dom = rc
        final, body = _fetch_page("https://" + dom)
        hosts = {m.group(1).lower() for m in _SH_RE.finditer(body or "")}
        if final:
            m = _SH_RE.search(final)
            if m:
                hosts.add(m.group(1).lower())
        return hosts

    harvested: set[str] = set()
    with cf.ThreadPoolExecutor(12) as ex:
        for hs in ex.map(harvest, live):
            harvested |= hs
    new_hosts = sorted(h for h in harvested if h not in taken)
    print(f"  harvested {len(harvested)} smarthub hosts, {len(new_hosts)} not yet in catalog")

    print("verifying siteName branding…")
    with cf.ThreadPoolExecutor(8) as ex:
        names = list(ex.map(fetch_sitename, new_hosts))

    report = {"confirmed": [], "review": [], "stats": {}}
    claimed: set[str] = set(taken)
    for host, name in zip(new_hosts, names):
        if not name or host in claimed:
            continue
        if " ".join(norm_tokens(name)) in GENERIC_SITENAMES:
            continue
        owners = [r for r in hostless if match_names(r["label"], name) == "confirmed"]
        if len(owners) == 1:
            r = owners[0]
            report["confirmed"].append({
                "code": r["code"], "label": r["label"], "state": r.get("state", ""),
                "host": host, "sitename": name, "file": r["_file"]})
            claimed.add(host)
        else:
            revs = [r for r in hostless if match_names(r["label"], name) == "review"]
            report["review"].append({
                "host": host, "sitename": name,
                "owners_confirmed": [r["code"] for r in owners],
                "owners_review": [r["code"] for r in revs][:5]})
    # one row must not be wired twice
    seen_codes: set[str] = set()
    uniq = []
    for v in report["confirmed"]:
        if v["code"] in seen_codes:
            report["review"].append({**v, "conflict": "row already wired this run"})
        else:
            seen_codes.add(v["code"])
            uniq.append(v)
    report["confirmed"] = uniq
    print(f"  {len(report['confirmed'])} CONFIRMED, {len(report['review'])} review")

    if args.apply and report["confirmed"]:
        sets: dict[str, dict[str, dict]] = {}
        for v in report["confirmed"]:
            sets.setdefault(v["file"], {})[v["code"]] = v
        n = 0
        for f in sorted(sets):
            with open(f, encoding="utf-8", newline="") as fh:
                rd = csv.DictReader(fh)
                fields = rd.fieldnames
                frows = list(rd)
            for row in frows:
                s = sets[f].get(row["code"])
                if s:
                    row["smarthub_host"] = s["host"]
                    row["portal_url"] = f"https://{s['host']}"
                    row["scrape_status"] = "live"
                    note = (row.get("notes") or "").strip()
                    stamp = (f"SmartHub host harvested from the co-op's own website + "
                             f"siteName-verified ('{s['sitename']}') Jul 2026.")
                    row["notes"] = (note + " " + stamp).strip()
                    n += 1
            with open(f, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                for row in frows:
                    row.pop("_file", None)
                    w.writerow(row)
        print(f"  applied: {n} hosts wired")

    report["stats"] = {
        "targets": len(targets), "domains_probed": len(cand),
        "domains_resolved": len(live), "hosts_harvested": len(harvested),
        "hosts_new": len(new_hosts), "confirmed": len(report["confirmed"]),
        "review": len(report["review"]),
    }
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "coop_site_harvest.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
