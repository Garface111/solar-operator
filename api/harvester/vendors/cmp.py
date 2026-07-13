"""Central Maine Power (CMP) — server-side Cloud Capture.

Bespoke Avangrid portal (NOT SmartHub). Public surface:
  * Marketing / Liferay shell: https://www.cmpco.com
  * SSO login:                 https://sso.cmpco.com/login
  * Customer portal:           https://portal.cmpco.com

After sign-in we navigate account/usage/bill pages and sniff JSON XHRs for
accounts + net-meter generation (same recon method as Eversource/GMP when
endpoints aren't published).

Provider code: cmp

Emits (when data is found):
  * generation → POST /v1/array-owners/utility-meter-capture
  * accounts   → POST /v1/sync

Limitations:
  * MFA / step-up auth: fails with a clear message; lockout guard pauses retries.
  * Usage field names evolve — sniffer keeps several generation aliases.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from .base import CaptureRequest, ScrapeResult

log = logging.getLogger("harvester.cmp")

LOGIN_URL = "https://sso.cmpco.com/login"
# Fallbacks if sso host is flaky from a given network
LOGIN_URL_ALT = "https://www.cmpco.com/c/portal/login"
HOME_URLS = (
    "https://portal.cmpco.com/",
    "https://portal.cmpco.com/group/customer",
    "https://www.cmpco.com/group/customer",
    "https://www.cmpco.com/web/customer",
    "https://portal.cmpco.com/group/customer/usage",
    "https://portal.cmpco.com/group/customer/bills",
    "https://www.cmpco.com/my-account",
    "https://www.cmpco.com/account",
)

_GEN_KEYS = (
    "returnedGeneration", "returned_generation", "generationKwh", "generation_kwh",
    "kwhGenerated", "kwh_generated", "generatedKwh", "generated_kwh",
    "solarGeneration", "solar_generation", "exportKwh", "export_kwh",
    "totalGrossGenerated", "grossGeneration", "energyExported", "energy_exported",
    "netGeneration", "productionKwh", "production_kwh", "kwhOut", "kwh_out",
    "deliveredKwh", "receivedKwh",  # AMI sometimes splits delivered/received
)
_DATE_KEYS = (
    "date", "day", "readDate", "read_date", "usageDate", "periodEnd",
    "period_end", "billingDate", "startDate", "endDate", "intervalDate",
    "usageDay", "readDt",
)
_ACCT_KEYS = (
    "accountNumber", "account_number", "accountNo", "acctNbr", "acctNumber",
    "account", "serviceAccountNumber", "premiseNumber", "contractAccount",
    "bpNumber", "businessPartner", "caNumber",  # SAP BP / contract account
)
_NICK_KEYS = (
    "nickname", "accountName", "account_name", "name", "label",
    "serviceAddress", "service_address", "address", "premiseAddress",
)


class CMPVendor:
    provider = "cmp"

    async def login_url(self, creds) -> str:
        return LOGIN_URL

    async def is_logged_in(self, page) -> bool:
        try:
            url = (page.url or "").lower()
            if any(x in url for x in (
                "/login", "signin", "sign-in", "sso.", "auth", "mfa", "okta",
            )):
                # Still on auth unless already inside the customer portal path.
                if "portal.cmpco" not in url and "/group/customer" not in url \
                        and "/my-account" not in url and "/web/customer" not in url:
                    return False
            pw = await page.query_selector('input[type="password"]')
            if pw and await pw.is_visible():
                return False
            try:
                marker = await page.evaluate(
                    """() => {
                      try {
                        for (const k of Object.keys(localStorage || {})) {
                          if (/token|session|okta|auth/i.test(k)
                              && (localStorage.getItem(k) || '').length > 20) return true;
                        }
                      } catch (e) {}
                      const c = document.cookie || '';
                      return /JSESSIONID|LFR_SESSION|CASTGC|session/i.test(c)
                          || /portal\\.cmpco|group\\/customer|my-account/i.test(location.href || '');
                    }"""
                )
                if marker:
                    return True
            except Exception:
                pass
            return ("cmpco.com" in url) and ("login" not in url)
        except Exception:
            return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        provider = "cmp"
        captured: list[tuple[str, Any]] = []

        async def _on_response(response):
            try:
                ct = (response.headers.get("content-type") or "").lower()
                url = response.url or ""
                if response.status >= 400:
                    return
                interesting = any(x in url.lower() for x in (
                    "api", "usage", "bill", "account", "customer", "meter",
                    "consumption", "interval", "greenbutton", "odata", "sap",
                ))
                if "json" not in ct and "javascript" not in ct \
                        and not url.endswith(".json") and not interesting:
                    return
                try:
                    body = await response.json()
                except Exception:
                    return
                captured.append((url, body))
            except Exception:
                return

        page.on("response", _on_response)

        # If engine left us on a dead sso page, try the Liferay login alt once.
        try:
            cur = (page.url or "").lower()
            if "login" in cur and not await self.is_logged_in(page):
                await page.goto(LOGIN_URL_ALT, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.5)
        except Exception as exc:  # noqa: BLE001
            log.warning("cmp alt login nav: %s", exc)

        for dest in HOME_URLS:
            try:
                await page.goto(dest, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2.5)
                if await self._is_mfa_wall(page):
                    raise RuntimeError(
                        "Central Maine Power requires multi-factor authentication "
                        "for this login. Cloud Capture can't complete MFA yet — "
                        "use a password-only service account if CMP allows it, "
                        "or establish a session via the browser helper."
                    )
                if not await self.is_logged_in(page):
                    await asyncio.sleep(2.0)
                    if not await self.is_logged_in(page):
                        raise RuntimeError(
                            "CMP session not authenticated after login "
                            "(wrong password, MFA, or portal redesign)."
                        )
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("cmp nav %s: %s", dest, exc)
                continue

        await asyncio.sleep(2.0)
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

        accounts = self._extract_accounts(captured)
        if not accounts:
            accounts = await self._dom_accounts(page)
        if not accounts:
            raise RuntimeError(
                "CMP login succeeded but no accounts/usage JSON was captured — "
                "portal API shape may have changed; needs a fresh HAR."
            )

        gen_accounts = [{
            "account_number": a["account_number"],
            "nickname": a.get("nickname") or "",
            "summary": a.get("summary") or {},
            "daily": a.get("daily") or [],
        } for a in accounts]

        requests: list[CaptureRequest] = []
        sync_accts = [{
            "accountNumber": a["account_number"],
            "nickname": a.get("nickname") or "",
            "serviceAddress": (a.get("summary") or {}).get("serviceAddress") or "",
            "solarNetMeter": bool(
                (a.get("daily") or [])
                or (a.get("summary") or {}).get("isNetMetered")
            ),
        } for a in gen_accounts]
        requests.append(CaptureRequest(
            path="/v1/sync",
            body={
                "provider": provider,
                "captureMethod": "cloud_capture",
                "capturedAt": datetime.utcnow().isoformat() + "Z",
                "pageUrl": page.url or LOGIN_URL,
                "user": {"username": creds.username, "utility": "cmp"},
                "auth": {"username": creds.username},
                "accounts": sync_accts,
                "bills": [],
                "usage": [],
            },
            note=f"cmp accounts ({len(sync_accts)})",
        ))

        gen_payload = [
            a for a in gen_accounts
            if a.get("daily") or (a.get("summary") or {}).get("totalGrossGenerated")
        ]
        if gen_payload:
            requests.append(CaptureRequest(
                path="/v1/array-owners/utility-meter-capture",
                body={"provider": provider, "accounts": gen_payload},
                note=f"cmp generation ({len(gen_payload)} accts)",
            ))

        solar_days = sum(len(a.get("daily") or []) for a in gen_payload)
        return ScrapeResult(
            requests=requests,
            summary=(
                f"{len(gen_accounts)} accounts, {len(gen_payload)} with gen, "
                f"{solar_days} solar days, {len(captured)} xhr"
            ),
        )

    @staticmethod
    async def _is_mfa_wall(page) -> bool:
        try:
            url = (page.url or "").lower()
            if "mfa" in url or ("verify" in url and ("okta" in url or "sso" in url)):
                return True
            body = ""
            try:
                body = (await page.inner_text("body"))[:4000].lower()
            except Exception:
                return False
            needles = (
                "verification code", "one-time", "enter the code",
                "authenticate", "push notification", "text message code",
                "sms code", "security code",
            )
            if any(n in body for n in needles) and "password" not in body[:200]:
                return True
            otp = await page.query_selector(
                'input[name*="code" i], input[autocomplete="one-time-code"], '
                'input[inputmode="numeric"]'
            )
            if otp and await otp.is_visible():
                return True
        except Exception:
            return False
        return False

    def _extract_accounts(self, captured: list[tuple[str, Any]]) -> list[dict]:
        by_acct: dict[str, dict] = {}
        for url, body in captured:
            self._walk(body, by_acct, url=url)
        out = []
        for a in by_acct.values():
            a.pop("_days", None)
            a["daily"] = sorted(a.get("daily") or [], key=lambda d: d.get("date") or "")
            out.append(a)
        return out

    def _walk(self, node: Any, by_acct: dict[str, dict], *, url: str = "",
              parent_acct: str | None = None) -> None:
        if isinstance(node, dict):
            acct = self._first_str(node, _ACCT_KEYS) or parent_acct
            nick = self._first_str(node, _NICK_KEYS)
            day = self._coerce_date(self._first_any(node, _DATE_KEYS))
            gen = self._first_num(node, _GEN_KEYS)
            # Prefer export/received over delivered when both present (net metering).
            if gen is None:
                recv = self._first_num(node, ("receivedKwh", "kwhReceived", "kWhReceived"))
                if recv is not None:
                    gen = recv
            if gen is None and "values" in node and isinstance(node["values"], list):
                for v in node["values"]:
                    self._walk(v, by_acct, url=url, parent_acct=acct)
            if gen is None and "intervals" in node and isinstance(node["intervals"], list):
                for v in node["intervals"]:
                    self._walk(v, by_acct, url=url, parent_acct=acct)

            if acct:
                row = by_acct.setdefault(acct, {
                    "account_number": acct,
                    "nickname": nick or "",
                    "summary": {},
                    "daily": [],
                    "_days": set(),
                })
                if nick and not row.get("nickname"):
                    row["nickname"] = nick
                if gen is not None and day and day not in row["_days"] and gen > 0:
                    row["daily"].append({"date": day, "generated_kwh": float(gen)})
                    row["_days"].add(day)
                period_gen = self._first_num(node, (
                    "totalGrossGenerated", "totalGeneration", "periodGeneration",
                    "ytdGeneration", "annualGeneration",
                ))
                if period_gen is not None and period_gen > 0:
                    row["summary"]["totalGrossGenerated"] = float(period_gen)
                    row["summary"]["isNetMetered"] = True

            for v in node.values():
                if isinstance(v, (dict, list)):
                    self._walk(v, by_acct, url=url, parent_acct=acct or parent_acct)
        elif isinstance(node, list):
            for item in node:
                self._walk(item, by_acct, url=url, parent_acct=parent_acct)

    async def _dom_accounts(self, page) -> list[dict]:
        try:
            text = await page.inner_text("body")
        except Exception:
            return []
        nums = re.findall(r"\b(\d{8,14})\b", text or "")
        seen: set[str] = set()
        out = []
        for n in nums:
            if n in seen:
                continue
            seen.add(n)
            out.append({
                "account_number": n,
                "nickname": "",
                "summary": {},
                "daily": [],
            })
            if len(out) >= 20:
                break
        return out

    @staticmethod
    def _first_str(d: dict, keys) -> str | None:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return str(d[k]).strip()
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            v = lower.get(k.lower())
            if v not in (None, ""):
                return str(v).strip()
        return None

    @staticmethod
    def _first_any(d: dict, keys):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            v = lower.get(k.lower())
            if v not in (None, ""):
                return v
        return None

    @staticmethod
    def _first_num(d: dict, keys) -> float | None:
        for k in keys:
            if k not in d:
                continue
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
        lower = {str(k).lower(): v for k, v in d.items()}
        for k in keys:
            v = lower.get(k.lower())
            if v is None or v == "":
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _coerce_date(val) -> str | None:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            try:
                ts = float(val)
                if ts > 1e12:
                    ts /= 1000.0
                return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                return None
        s = str(val).strip()
        if not s:
            return None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return None
