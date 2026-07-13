"""Eversource Energy (CT / MA / NH) — server-side Cloud Capture.

Bespoke portal (NOT SmartHub). Login is the public MyAccount / security form at
www.eversource.com (Okta-backed MFA may appear for some accounts). After sign-in
we land on /CG/Customer/* and sniffs JSON XHRs for accounts + usage / generation
(the same recon method used for GMP/Chint when endpoints aren't published).

Provider codes accepted: eversource, eversource_ma, eversource_ct (regional
catalog rows; one portal, one module).

Emits (when data is found):
  * generation → POST /v1/array-owners/utility-meter-capture
  * accounts   → POST /v1/sync (so UtilityAccount rows exist for billing UX)

Limitations (flagged LOUDLY):
  * MFA wall: if the account requires email/SMS MFA and no warm session exists,
    harvest fails with a clear login_failed detail (lockout guard pauses retries).
  * Usage field names evolve — the sniffer keeps several generation aliases.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from .base import CaptureRequest, ScrapeResult

log = logging.getLogger("harvester.eversource")

LOGIN_URL = (
    "https://www.eversource.com/security/account/login"
    "?ReturnUrl=/cg/customer/"
)
HOME_URLS = (
    "https://www.eversource.com/cg/customer/",
    "https://www.eversource.com/CG/Customer/MyAccount",
    "https://www.eversource.com/CG/Customer/accountoverview",
    "https://www.eversource.com/cg/customer/usage",
    "https://www.eversource.com/cg/customer/bills",
    "https://www.eversource.com/CG/Customer/BillHistory",
)

# JSON body keys that often carry exported / generated kWh for net-metered solar.
_GEN_KEYS = (
    "returnedGeneration", "returned_generation", "generationKwh", "generation_kwh",
    "kwhGenerated", "kwh_generated", "generatedKwh", "generated_kwh",
    "solarGeneration", "solar_generation", "exportKwh", "export_kwh",
    "totalGrossGenerated", "grossGeneration", "energyExported", "energy_exported",
    "netGeneration", "productionKwh", "production_kwh",
)
_CONSUME_KEYS = (
    "consumed", "consumption", "consumptionKwh", "kwhConsumed", "usageKwh",
    "energyUsed", "energy_used",
)
_DATE_KEYS = ("date", "day", "readDate", "read_date", "usageDate", "periodEnd",
              "period_end", "billingDate", "startDate", "endDate", "intervalDate")
_ACCT_KEYS = (
    "accountNumber", "account_number", "accountNo", "acctNbr", "acctNumber",
    "account", "serviceAccountNumber", "premiseNumber", "contractAccount",
)
_NICK_KEYS = ("nickname", "accountName", "account_name", "name", "label",
              "serviceAddress", "service_address", "address")

_PROVIDER_ALIASES = frozenset({"eversource", "eversource_ma", "eversource_ct"})


class EversourceVendor:
    provider = "eversource"

    async def login_url(self, creds) -> str:
        return LOGIN_URL

    async def is_logged_in(self, page) -> bool:
        try:
            url = (page.url or "").lower()
            if any(x in url for x in ("/login", "signin", "sign-in", "mfalogin",
                                      "mfa", "okta.com")):
                # Still on auth surface unless /cg/customer is already mixed in.
                if "/cg/customer" not in url and "/cg/" not in url:
                    return False
            pw = await page.query_selector('input[type="password"]')
            if pw and await pw.is_visible():
                return False
            # Cookie / local session markers after a successful MyAccount login.
            try:
                marker = await page.evaluate(
                    """() => {
                      try {
                        const u = (localStorage.getItem('okta-token-storage') || '');
                        if (u && u.length > 20) return true;
                      } catch (e) {}
                      return document.cookie.includes('AspNet')
                          || document.cookie.toLowerCase().includes('session')
                          || (location.pathname || '').toLowerCase().includes('/cg/');
                    }"""
                )
                if marker:
                    return True
            except Exception:
                pass
            # Fallback: no password field and not on a login host.
            return "eversource.com" in url and "login" not in url
        except Exception:
            return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        provider = (creds.provider or "eversource").strip().lower()
        if provider not in _PROVIDER_ALIASES:
            provider = "eversource"

        captured: list[tuple[str, Any]] = []

        async def _on_response(response):
            try:
                ct = (response.headers.get("content-type") or "").lower()
                url = response.url or ""
                if response.status >= 400:
                    return
                if "json" not in ct and "javascript" not in ct and not url.endswith(".json"):
                    # Still try small JSON bodies without a content-type.
                    if "api" not in url.lower() and "esapi" not in url.lower() \
                            and "usage" not in url.lower() and "bill" not in url.lower() \
                            and "account" not in url.lower() and "customer" not in url.lower():
                        return
                # Cap body size
                try:
                    body = await response.json()
                except Exception:
                    return
                captured.append((url, body))
            except Exception:
                return

        page.on("response", _on_response)

        # Warm the customer SPA so account/usage XHRs fire.
        for dest in HOME_URLS:
            try:
                await page.goto(dest, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2.5)
                # MFA / login bounce detection after navigation
                if await self._is_mfa_wall(page):
                    raise RuntimeError(
                        "Eversource requires multi-factor authentication for this "
                        "login. Cloud Capture can't complete MFA yet — use the "
                        "browser helper once to establish a session, or disable MFA "
                        "for a service account if Eversource allows it."
                    )
                if not await self.is_logged_in(page):
                    # First dest after a cold start might bounce to login; engine
                    # should have already logged in. Re-check after a beat.
                    await asyncio.sleep(2.0)
                    if not await self.is_logged_in(page):
                        raise RuntimeError(
                            "Eversource session not authenticated after login "
                            "(wrong password, MFA, or portal redesign)."
                        )
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("eversource nav %s: %s", dest, exc)
                continue

        # Give late XHRs a moment
        await asyncio.sleep(2.0)
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

        accounts = self._extract_accounts(captured)
        if not accounts:
            # DOM fallback: scrape any visible account numbers on MyAccount.
            accounts = await self._dom_accounts(page)
        if not accounts:
            raise RuntimeError(
                "Eversource login succeeded but no accounts/usage JSON was "
                "captured — portal API shape may have changed; needs a fresh HAR."
            )

        gen_accounts = []
        for acct in accounts:
            daily = acct.get("daily") or []
            gen_accounts.append({
                "account_number": acct["account_number"],
                "nickname": acct.get("nickname") or "",
                "summary": acct.get("summary") or {},
                "daily": daily,
            })

        requests: list[CaptureRequest] = []
        # Always register accounts via /v1/sync so the owner sees utility meters
        # even when daily generation wasn't in the sniffed payload yet.
        sync_accts = [{
            "accountNumber": a["account_number"],
            "nickname": a.get("nickname") or "",
            "serviceAddress": (a.get("summary") or {}).get("serviceAddress") or "",
            "solarNetMeter": bool((a.get("daily") or []) or
                                  (a.get("summary") or {}).get("isNetMetered")),
        } for a in gen_accounts]
        requests.append(CaptureRequest(
            path="/v1/sync",
            body={
                "provider": provider,
                "captureMethod": "cloud_capture",
                "capturedAt": datetime.utcnow().isoformat() + "Z",
                "pageUrl": page.url or LOGIN_URL,
                "user": {
                    "username": creds.username,
                    "utility": "eversource",
                },
                "auth": {"username": creds.username},
                "accounts": sync_accts,
                "bills": [],
                "usage": [],
            },
            note=f"eversource accounts ({len(sync_accts)})",
        ))

        # Generation only when we have at least one day or a period total.
        gen_payload = []
        for a in gen_accounts:
            if a.get("daily") or (a.get("summary") or {}).get("totalGrossGenerated"):
                gen_payload.append(a)
        if gen_payload:
            requests.append(CaptureRequest(
                path="/v1/array-owners/utility-meter-capture",
                body={"provider": provider, "accounts": gen_payload},
                note=f"eversource generation ({len(gen_payload)} accts)",
            ))

        solar_days = sum(len(a.get("daily") or []) for a in gen_payload)
        return ScrapeResult(
            requests=requests,
            summary=(
                f"{len(gen_accounts)} accounts, {len(gen_payload)} with gen, "
                f"{solar_days} solar days, {len(captured)} xhr"
            ),
        )

    # ── detection helpers ─────────────────────────────────────────────────────

    @staticmethod
    async def _is_mfa_wall(page) -> bool:
        try:
            url = (page.url or "").lower()
            if "mfa" in url or "verify" in url and "okta" in url:
                return True
            # Visible MFA challenge copy
            body = ""
            try:
                body = (await page.inner_text("body"))[:4000].lower()
            except Exception:
                return False
            needles = (
                "verification code", "one-time", "enter the code",
                "authenticate", "push notification", "okta verify",
                "text message code", "sms code",
            )
            if any(n in body for n in needles) and "password" not in body[:200]:
                return True
            # OTP input
            otp = await page.query_selector(
                'input[name*="code" i], input[autocomplete="one-time-code"], '
                'input[inputmode="numeric"]'
            )
            if otp and await otp.is_visible():
                return True
        except Exception:
            return False
        return False

    # ── JSON sniffer ──────────────────────────────────────────────────────────

    def _extract_accounts(self, captured: list[tuple[str, Any]]) -> list[dict]:
        """Walk every captured JSON payload and pull account + daily generation."""
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
            # Some payloads nest values under "values" / "intervals"
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
                if gen is not None and day and day not in row["_days"]:
                    if gen > 0:
                        row["daily"].append({"date": day, "generated_kwh": float(gen)})
                        row["_days"].add(day)
                # Period totals on summary objects
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
        """Last-resort: scrape account-looking numbers from the visible page."""
        try:
            text = await page.inner_text("body")
        except Exception:
            return []
        # 8–12 digit account numbers common at Eversource
        nums = re.findall(r"\b(\d{8,12})\b", text or "")
        seen = set()
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

    # ── field helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _first_str(d: dict, keys) -> str | None:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return str(d[k]).strip()
        # case-insensitive
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
            # epoch ms or s
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
        # ISO / date-only
        m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if m:
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return None
