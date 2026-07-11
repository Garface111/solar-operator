"""GMP (Green Mountain Power) — server-side capture.

Ported from gmp_meter_content.js + the GMP_FETCH_USAGE proxy in background.js.
The extension routes the GMP API through its service worker only to dodge the
content-script CORS block; server-side there is no CORS, so after login we lift
the JWT from localStorage['gmp-vue'] and call api.greenmountainpower.com directly.

Emits two captures, identical to the extension:
  * generation → POST /v1/array-owners/utility-meter-capture
  * bill/JWT   → POST /v1/sync  (so the backend stores a UtilitySession + pulls
                 historical bill PDFs with the JWT)
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .base import CaptureRequest, ScrapeResult

log = logging.getLogger("harvester.gmp")

API = "https://api.greenmountainpower.com/api/v2"
LOGIN_URL = "https://greenmountainpower.com/account/login/"
MAX_YEARS = 12

_HEADERS_TMPL = {"Accept": "application/json", "GMP-Source": "web"}

# Read the Vue app's persisted store — the JWT + account map live here.
_READ_STORE_JS = "() => { try { return localStorage.getItem('gmp-vue'); } catch (e) { return null; } }"


class GMPVendor:
    provider = "gmp"

    async def login_url(self, creds) -> str:
        return LOGIN_URL

    async def _store(self, page) -> dict | None:
        raw = await page.evaluate(_READ_STORE_JS)
        if not raw:
            return None
        try:
            import json
            return json.loads(raw)
        except Exception:
            return None

    async def is_logged_in(self, page) -> bool:
        store = await self._store(page)
        try:
            return bool(store and store.get("user", {}).get("apitoken"))
        except Exception:
            return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        # SPA render race: the token isn't in localStorage the instant login
        # lands. Poll a short while (mirrors content.js 5s×12).
        store = None
        import asyncio
        for _ in range(24):
            store = await self._store(page)
            if store and store.get("user", {}).get("apitoken"):
                break
            await asyncio.sleep(2.0)
        if not store:
            raise RuntimeError("gmp-vue store never populated after login")

        user = store.get("user", {}) or {}
        jwt = user.get("apitoken")
        if not jwt:
            raise RuntimeError("no apitoken in gmp-vue store (login incomplete)")

        headers = {**_HEADERS_TMPL, "Authorization": f"Bearer {jwt}"}
        requests: list[CaptureRequest] = []

        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0), headers=headers) as api:
            # 1) accounts
            cur = await self._get(api, "/users/current")
            accounts_meta = ((cur.get("customData") or {}).get("energyAccounts")
                             or cur.get("energyAccounts") or [])
            gen_accounts = []
            solar_days = 0
            for acct in accounts_meta:
                acct_no = str(acct.get("accountNumber") or acct.get("account_number") or "").strip()
                if not acct_no:
                    continue
                nickname = acct.get("nickname") or acct.get("name") or ""
                summary = await self._get(api, f"/usage/{acct_no}/summary") or {}
                is_solar = bool(
                    summary.get("isNetMetered")
                    or (summary.get("totalGrossGenerated") or 0) > 0
                    or (summary.get("totalGenerationSentToGrid") or 0) > 0
                    or (summary.get("totalGenerationUsedByHome") or 0) > 0
                )
                daily = await self._daily_backfill(api, acct_no) if is_solar else []
                solar_days += len(daily)
                gen_accounts.append({
                    "account_number": acct_no,
                    "nickname": nickname,
                    "summary": summary,
                    "daily": daily,
                })

            if gen_accounts:
                requests.append(CaptureRequest(
                    path="/v1/array-owners/utility-meter-capture",
                    body={"provider": "gmp", "accounts": gen_accounts},
                    note=f"gmp generation ({len(gen_accounts)} accts, {solar_days} days)",
                ))

        # 2) bill / JWT sync — hand the JWT + current-bill URLs to the backend,
        #    which enumerates & pulls historical PDFs itself.
        sync_accounts = []
        for a in (user.get("accounts") or []):
            sync_accounts.append({
                "accountNumber": a.get("accountNumber"),
                "nickname": a.get("nickname"),
                "customerNumber": a.get("personId") or a.get("customerNumber"),
                "currentBillUrl": a.get("currentBillUrl"),
                "currentBillUrlBinary": a.get("currentBillUrlBinary"),
                "serviceAddress": a.get("address") or a.get("serviceAddress"),
                "solarNetMeter": a.get("solarNetMeter"),
                "groupNetMetered": a.get("groupNetMetered"),
                "isPrimary": a.get("isPrimary"),
            })
        requests.append(CaptureRequest(
            path="/v1/sync",
            body={
                "provider": "gmp",
                "capturedAt": datetime.utcnow().isoformat() + "Z",
                "pageUrl": LOGIN_URL,
                "user": {
                    "accountId": user.get("accountId"),
                    "username": user.get("username"),
                    "email": user.get("email"),
                    "fullName": user.get("fullName"),
                },
                "auth": {
                    "apiToken": jwt,
                    "apiTokenExpires": user.get("apitokenExpires"),
                    "refreshToken": user.get("refreshtoken"),
                },
                "accounts": sync_accounts,
            },
            note=f"gmp bill/jwt sync ({len(sync_accounts)} accts)",
        ))

        return ScrapeResult(
            requests=requests,
            summary=f"{len(gen_accounts)} accounts, {solar_days} solar days",
        )

    # ── helpers ────────────────────────────────────────────────────────────────
    @staticmethod
    async def _get(api: httpx.AsyncClient, path: str) -> dict:
        r = await api.get(f"{API}{path}")
        if r.status_code == 401:
            raise RuntimeError("gmp API 401 — JWT expired (re-auth needed)")
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {}

    async def _daily_backfill(self, api: httpx.AsyncClient, acct_no: str) -> list[dict]:
        """Walk /usage/{acct}/daily one calendar year at a time, newest→oldest,
        stopping at the first EMPTY prior year (the pre-online void). Keeps only
        returnedGeneration>0 rows. Mirrors background.js:807-872."""
        out: list[dict] = []
        this_year = datetime.utcnow().year
        for i in range(MAX_YEARS):
            year = this_year - i
            start = f"{year}-01-01T00:00:00"
            end = (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
                   if year == this_year else f"{year}-12-31T23:59:59")
            try:
                resp = await api.get(
                    f"{API}/usage/{acct_no}/daily",
                    params={"startDate": start, "endDate": end, "temp": "f"},
                )
                resp.raise_for_status()
                data = resp.json() or {}
            except Exception:
                break
            rows = []
            for interval in (data.get("intervals") or []):
                for v in (interval.get("values") or []):
                    gen = v.get("returnedGeneration")
                    if gen and gen > 0 and v.get("date"):
                        rows.append({"date": str(v["date"])[:10], "generated_kwh": gen})
            if not rows and year != this_year:
                break                                  # pre-online void → stop
            out.extend(rows)
        return out
