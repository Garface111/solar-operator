"""SmartHub (NISC) co-ops — server-side capture. Generic across ~530 *.smarthub.coop.

Ported from smarthub_content.js. The NISC "secured" API authenticates ONLY with
the owner's httpOnly session cookie — it cannot be replayed headlessly, which is
the whole reason a real browser is required. Playwright's ``context.request``
shares the logged-in BrowserContext's cookie jar, so the secured GETs/POSTs run
same-origin with the session cookie, exactly like the content script did.

Emits:
  * bills      → POST /v1/sync (batched by 5, backend dedups by uuid)
  * generation → POST /v1/array-owners/utility-meter-capture
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta

from .base import CaptureRequest, ScrapeResult

log = logging.getLogger("harvester.smarthub")

USAGE_CHUNK_DAYS = 90            # NISC truncates a single 18-month DAILY POST
USAGE_LOOKBACK_DAYS = 580       # ~19 months
PDF_WINDOW_DAYS = 400           # pull PDFs for the last ~13 months (billing-relevant)
PDF_MIN, PDF_MAX = 1024, 12 * 1024 * 1024


def _mdy(ts) -> str:
    """NISC epoch-millis → 'M/D/YYYY' (mirrors the extension's fmtDateMDY). The
    backend bill-date regex AND our own _pdf() both strptime this, and _parse_amount
    parity aside, billing_date MUST be a date STRING, never the raw number."""
    if not isinstance(ts, (int, float)):
        return ""
    try:
        d = datetime.utcfromtimestamp(ts / 1000.0 if ts > 1e11 else ts)
        return f"{d.month}/{d.day}/{d.year}"
    except (ValueError, OSError, OverflowError):
        return ""


def _iso(ts) -> str | None:
    """NISC epoch-millis → 'YYYY-MM-DD' (mirrors the extension's isoDay). The bill's
    meter-read period; the backend parses it with fromisoformat, so a raw epoch would
    land as NULL — which is exactly what left offtakers with 'no bill on file'."""
    if not isinstance(ts, (int, float)):
        return None
    try:
        d = datetime.utcfromtimestamp(ts / 1000.0 if ts > 1e11 else ts)
        return d.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


class SmartHubVendor:
    provider = "smarthub"       # registry maps any co-op code here

    async def login_url(self, creds) -> str:
        host = (creds.login_host or "").strip()
        if not host:
            raise RuntimeError("smarthub credential missing login_host (co-op subdomain)")
        return f"https://{host}/"

    async def is_logged_in(self, page) -> bool:
        try:
            url = (page.url or "").lower()
            if any(x in url for x in ("/login", "signin", "sign-in")):
                return False
            # A visible password field ⇒ still on the login screen.
            pw = await page.query_selector('input[type="password"]')
            if pw and await pw.is_visible():
                return False
            return True
        except Exception:
            return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        host = (creds.login_host or "").strip()
        base = f"https://{host}"
        email = creds.username
        req = context.request                      # shares the cookie jar

        # Let the SPA settle so the session cookie + hash are in place.
        import asyncio
        await asyncio.sleep(2.0)
        user_id = await self._user_id(page, email)

        # 1) discover accounts (x-nisc header required)
        accts = await self._accounts(req, base, email)
        if not accts:
            raise RuntimeError("no SmartHub accounts discoverable (session/discovery race)")

        provider_code = (creds.provider or "smarthub").lower()
        bills: list[dict] = []
        gen_accounts: list[dict] = []
        cutoff = datetime.utcnow() - timedelta(days=PDF_WINDOW_DAYS)

        for a in accts:
            acct_no = str(a.get("account") or a.get("acctNbr") or "").strip()
            if not acct_no:
                continue
            overview = await self._overview(req, base, acct_no)
            for row in overview:
                bill = self._bill_row(acct_no, row)
                # Pull the PDF for recent, billing-relevant bills only.
                if bill.get("billing_date") and self._within(bill["billing_date"], cutoff):
                    pdf = await self._pdf(req, base, bill)
                    if pdf:
                        bill["pdf_b64"] = pdf
                bills.append(bill)

            # generation (best-effort — never let a userId miss kill the bills)
            try:
                srv_loc = self._srv_loc(overview)
                daily = await self._generation(req, base, user_id, acct_no, srv_loc)
                if daily:
                    gen_accounts.append({
                        "account_number": acct_no,
                        "nickname": self._nickname(overview),
                        "summary": {},
                        "daily": daily,
                    })
            except Exception as exc:                 # noqa: BLE001
                log.warning("smarthub generation skipped: %s", type(exc).__name__)

        requests: list[CaptureRequest] = []

        # bills in batches of 5 (accounts repeated per batch, idempotent upsert)
        acct_summ = [{"accountNumber": str(a.get("account") or ""),
                      "customerName": "", "serviceAddress": ""} for a in accts]
        for i in range(0, len(bills), 5):
            batch = bills[i:i + 5]
            requests.append(CaptureRequest(
                path="/v1/sync",
                body={
                    "provider": provider_code,
                    "captureMethod": "api",
                    "capturedAt": datetime.utcnow().isoformat() + "Z",
                    "pageUrl": base + "/",
                    "user": {"hostname": host, "utility": provider_code, "username": email},
                    "auth": {"username": email},
                    "accounts": acct_summ,
                    "bills": batch,
                    "usage": [],
                },
                note=f"{provider_code} bills {i}-{i+len(batch)}",
            ))

        if gen_accounts:
            requests.append(CaptureRequest(
                path="/v1/array-owners/utility-meter-capture",
                body={"provider": provider_code, "accounts": gen_accounts},
                note=f"{provider_code} generation ({len(gen_accounts)} accts)",
            ))

        return ScrapeResult(
            requests=requests,
            summary=f"{len(accts)} accts, {len(bills)} bills, {len(gen_accounts)} gen accts",
        )

    # ── secured API ─────────────────────────────────────────────────────────────
    @staticmethod
    async def _accounts(req, base: str, email: str) -> list[dict]:
        try:
            r = await req.get(f"{base}/services/secured/accounts",
                              params={"user": email},
                              headers={"x-nisc-smarthub-username": email})
            if r.ok:
                data = await r.json()
                return data if isinstance(data, list) else (data.get("accounts") or [])
        except Exception as exc:                     # noqa: BLE001
            log.warning("accounts discovery failed: %s", exc)
        return []

    @staticmethod
    async def _overview(req, base: str, acct_no: str) -> list[dict]:
        try:
            r = await req.get(f"{base}/services/secured/billing/history/overview",
                              params={"acctNbr": acct_no})
            if r.ok:
                data = await r.json()
                return data if isinstance(data, list) else (data.get("bills") or data.get("rows") or [])
        except Exception as exc:                     # noqa: BLE001
            log.warning("overview failed: %s", type(exc).__name__)
        return []

    async def _pdf(self, req, base: str, bill: dict) -> str | None:
        uuid = bill.get("bill_uuid")
        ts = bill.get("bill_timestamp")
        acct = bill.get("account_id")
        sor = bill.get("system_of_record")
        bdate = bill.get("billing_date") or ""
        if not (uuid and acct):
            return None
        try:
            d = datetime.strptime(bdate, "%m/%d/%Y").strftime("%Y_%m_%d")
        except Exception:
            d = "bill"
        url = f"{base}/services/secured/billPdfService/{d}_{acct}.pdf"
        try:
            r = await req.get(url, params={"account": acct, "timestamp": ts,
                                           "uuid": uuid, "systemOfRecord": sor})
            if not r.ok:
                return None
            body = await r.body()
            if not (PDF_MIN <= len(body) <= PDF_MAX):
                return None
            return base64.b64encode(body).decode("ascii")
        except Exception as exc:                     # noqa: BLE001
            log.warning("pdf pull failed for %s: %s", uuid, exc)
            return None

    async def _generation(self, req, base, user_id, acct_no, srv_loc) -> list[dict]:
        if not srv_loc:
            return []
        gen_by_day: dict[str, float] = {}
        end = datetime.utcnow()
        for _ in range(0, USAGE_LOOKBACK_DAYS, USAGE_CHUNK_DAYS):
            start = end - timedelta(days=USAGE_CHUNK_DAYS)
            body = {
                "timeFrame": "DAILY", "userId": user_id, "screen": "USAGE_COMPARISON",
                "includeDemand": False, "serviceLocationNumber": srv_loc,
                "accountNumber": acct_no, "industries": ["ELECTRIC"],
                "startDateTime": int(start.timestamp() * 1000),
                "endDateTime": int(end.timestamp() * 1000),
            }
            try:
                r = await req.post(f"{base}/services/secured/utility-usage",
                                   data=body,
                                   headers={"Content-Type": "application/json",
                                            "x-nisc-smarthub-username": user_id})
                if r.ok:
                    self._reduce(await r.json(), gen_by_day)
            except Exception as exc:                 # noqa: BLE001
                log.warning("utility-usage chunk failed: %s", exc)
            end = start
        return [{"date": d, "generated_kwh": round(v, 3)}
                for d, v in sorted(gen_by_day.items()) if v > 0]

    @staticmethod
    def _reduce(resp: dict, out: dict) -> None:
        """NEGATIVE daily y = net export = generation. Sum per ISO day. Do NOT
        filter on meter flags (grounded on West Glover acct 6578300)."""
        for series_wrap in (resp.get("ELECTRIC") or []):
            for series in (series_wrap.get("series") or []):
                for pt in (series.get("data") or []):
                    x, y = pt.get("x"), pt.get("y")
                    if x is None or y is None or y >= 0:
                        continue
                    try:
                        day = datetime.utcfromtimestamp(x / 1000).strftime("%Y-%m-%d")
                    except (OverflowError, OSError, ValueError):
                        continue
                    out[day] = out.get(day, 0.0) + abs(y)

    # ── shaping ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _bill_row(acct_no: str, row: dict) -> dict:
        servloc = (row.get("servLocs") or [{}])[0]
        addr = servloc.get("address") or {}
        # NISC serves epoch-millis, not date strings, and amounts as numbers. Mirror
        # the extension's proven shape EXACTLY (smarthub_content.js): dates → strings,
        # amounts → strings. Raw values here 500'd the /v1/sync bill batch and left
        # periods NULL — the "no VEC bill on file" bug (Ford 2026-07-11).
        bts = row.get("billingDateTimestamp")
        amt = row.get("adjustedBillAmount")
        adj = row.get("totalAdjustments")
        return {
            "account_id": row.get("acctNbr") or acct_no,
            "customer_name": row.get("custName") or "",
            "service_address": ", ".join(filter(None, [
                addr.get("addr1"), addr.get("city"), addr.get("state"), addr.get("zip")])),
            "billing_date": _mdy(bts) or (row.get("billingDate") or ""),
            "bill_amount": None if amt is None else str(amt),
            "adjustments": None if adj is None else str(adj),
            "total_due": None if amt is None else str(amt),
            "kwh": row.get("totalUsage") if isinstance(row.get("totalUsage"), (int, float)) else None,
            "period_start": _iso(servloc.get("lastBillPrevReadDtTm")),
            "period_end": _iso(servloc.get("lastBillPresReadDtTm")),
            "bill_uuid": row.get("billProcessUuid"),
            "bill_timestamp": str(bts) if bts else None,
            "system_of_record": row.get("systemOfRecord"),
            "customer_number": row.get("custNbr"),
            "source": "cloud_capture",
        }

    @staticmethod
    def _srv_loc(overview: list[dict]):
        for row in overview:
            sl = (row.get("servLocs") or [{}])[0]
            n = (sl.get("id") or {}).get("srvLocNbr") or sl.get("srvLocNbr")
            if n:
                return n
        return None

    @staticmethod
    def _nickname(overview: list[dict]) -> str:
        for row in overview:
            sl = (row.get("servLocs") or [{}])[0]
            addr = sl.get("address") or {}
            if addr.get("addr1"):
                return ", ".join(filter(None, [addr.get("addr1"), addr.get("city")]))
        return ""

    @staticmethod
    def _within(billing_date: str, cutoff: datetime) -> bool:
        try:
            return datetime.strptime(billing_date, "%m/%d/%Y") >= cutoff
        except Exception:
            return True                              # unknown → pull (safe default)

    @staticmethod
    async def _user_id(page, fallback: str) -> str:
        """utility-usage needs the NISC userId. The SPA base64-encodes it in the
        #/home hash; decode it, else fall back to the email."""
        try:
            h = await page.evaluate("() => location.hash || ''")
            if h and "?" in h:
                import base64 as _b, urllib.parse as _u
                blob = h.split("?", 1)[1]
                try:
                    decoded = _b.b64decode(blob + "===").decode("utf-8", "ignore")
                    q = _u.parse_qs(decoded)
                    for k in ("userId", "user", "primaryUsername"):
                        if q.get(k):
                            return q[k][0]
                except Exception:
                    pass
        except Exception:
            pass
        return fallback
