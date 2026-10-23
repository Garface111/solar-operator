"""Chint / CPS "Monitor" (provider "chint") — server-side live capture.

Ported from chint_content.js + chint_inject.js. Two hard constraints make Chint
the trickiest port:

  1. NON-REPLAYABLE TOKEN. The SPA signs each /api/asset/* request with a
     per-request CryptoJS token that is NOT in the cookie jar; replaying it 4010s.
     So we NEVER call the API ourselves — we drive the SPA's own hash routes and
     PASSIVELY OBSERVE its authenticated responses (Playwright page.expect_response).

  2. VISIBLE-TAB-ONLY. The Chint SPA only renders + fetches in a VISIBLE tab; a
     hidden / zero-viewport / old-headless browser captures NOTHING. ⚠️ DEPLOY
     NOTE: run the harvester with a REAL render for Chint — headless=new (default
     in recent Chromium) with a real viewport, or headed under Xvfb, plus
     bring_to_front(). If Chint captures 0 sites in prod, this is why — the SPA is
     gating on document.visibilityState. This is the #1 Chint port risk and needs
     live verification against a real Chint account.

The walk: #/pv/sites fires /api/asset/site/retrieve (the site list); each
#/pv/sites/siteDetail/<id> fires /api/asset/site/busTypeDevices (that site's
inverters). Re-entered every cycle so the SPA re-fetches (freshness).
"""
from __future__ import annotations

import logging
import re
import weakref

from .base import CaptureRequest, ScrapeResult

log = logging.getLogger("harvester.chint")

BASE = "https://monitor.chintpowersystems.com/"
RETRIEVE = "/api/asset/site/retrieve"
DEVICES = "/api/asset/site/busTypeDevices"
LOGIN_API = "/api/users/user/doLogin"
PER_SITE_TIMEOUT_MS = 7000

# ── Chint's own verdict on a sign-in ─────────────────────────────────────────
# The portal's login page POSTs /api/users/user/doLogin and branches on the JSON
# it gets back (read straight from the SPA bundle, main.*.js `loginAccount`,
# 2026-10-23). A CORRECT password can still leave the visitor on the login page:
#
#   success:false                       → wrong email/password (msg says why)
#   data.checkUserList non-empty        → this email has several Chint accounts;
#                                         the page asks which one to open
#   data.checkOtp                       → two-step sign-in: a code texted to
#                                         data.mobile must be typed in
#   data.agreeResult falsy (otherwise)  → the account has never accepted Chint's
#                                         EULA; a modal asks for it, and only then
#                                         does the SPA re-login with agreeEULA=1
#   data.agreeResult truthy             → signed in
#
# Without reading this the harvester only saw "the password box is still on
# screen" and charged a correct password to the lockout counter: Bruce's
# brand-new Chint customer account (created by Chint support 2026-10-19, never
# signed into by a person) paused as "re-enter your password" while the password
# was fine and only the one-time EULA stood in the way. We NEVER accept the EULA
# (or pick an account, or read a texted code) on the owner's behalf — same rule
# as Fronius's terms wall — we name the wall so it is a one-step instruction.
_LOGIN_VERDICTS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def login_verdict(body) -> dict | None:
    """Classify a doLogin JSON response. Pure; see the table above."""
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    if not body.get("success"):
        msg = str(body.get("msg") or "").strip()
        return {"kind": "rejected", "msg": msg[:160]}
    users = data.get("checkUserList") or []
    if isinstance(users, list) and users:
        return {"kind": "pick_account", "accounts": len(users)}
    if data.get("checkOtp"):
        return {"kind": "otp"}
    if data.get("agreeResult"):
        return {"kind": "ok"}
    return {"kind": "eula"}


_PORTAL = "monitor.chintpowersystems.com"
_BLOCK_MESSAGES = {
    "eula": ("Chint needs this login to accept its user agreement once. Sign in at "
             f"{_PORTAL}, accept the agreement, and automatic refresh resumes on its own."),
    "otp": ("Chint is asking this login for a code texted to the account's phone "
            "(two-step sign-in), which automatic refresh can't type. Turn off SMS "
            f"verification for this account at {_PORTAL} (or ask Chint support) and "
            "refresh resumes on its own."),
}


def block_message(verdict: dict | None) -> str | None:
    """The owner-facing instruction for a human-only wall, or None when the
    verdict is a plain success or a real wrong-password rejection."""
    if not verdict:
        return None
    kind = verdict.get("kind")
    if kind == "pick_account":
        n = verdict.get("accounts") or "several"
        return (f"This email opens {n} different Chint accounts, and Chint asks which "
                "one to use at every sign-in, so automatic refresh can't choose. Ask "
                "Chint support to merge them into one, or save the login of the "
                "account that owns your sites.")
    return _BLOCK_MESSAGES.get(kind)


class ChintVendor:
    provider = "chint"

    async def login_url(self, creds) -> str:
        return BASE

    async def prepare_page(self, page) -> None:
        """Before anything loads, listen for Chint's doLogin answer so a failed
        sign-in can say WHY (see login_verdict). Never reads or logs the
        password; only the response JSON."""
        state: dict = {}
        try:
            _LOGIN_VERDICTS[page] = state
        except TypeError:
            return

        async def _on_response(resp):
            try:
                if LOGIN_API not in (resp.url or ""):
                    return
                state["verdict"] = login_verdict(await resp.json())
            except Exception:
                pass

        page.on("response", _on_response)

    def _verdict(self, page) -> dict | None:
        try:
            return (_LOGIN_VERDICTS.get(page) or {}).get("verdict")
        except TypeError:
            return None

    async def login_block_reason(self, page) -> str | None:
        """A human-only wall after Chint ACCEPTED the password (EULA, account
        picker, texted code). The engine records it as login_blocked, which does
        not count toward the lockout pause."""
        return block_message(self._verdict(page))

    async def login_failure_detail(self, page) -> str | None:
        """Chint's own words when it rejected the sign-in, for the audit row."""
        v = self._verdict(page)
        if v and v.get("kind") == "rejected":
            return "Chint rejected the sign-in" + (f": {v['msg']}" if v.get("msg") else "")
        return None

    async def is_logged_in(self, page) -> bool:
        try:
            pw = await page.query_selector('input[type="password"]')
            return not (pw and await pw.is_visible())
        except Exception:
            return False

    async def scrape(self, page, context, creds) -> ScrapeResult:
        try:
            await page.bring_to_front()               # the SPA only fetches when visible
        except Exception:
            pass

        # 1) force the site list route → observe /api/asset/site/retrieve
        try:
            async with page.expect_response(
                    lambda r: RETRIEVE in r.url, timeout=PER_SITE_TIMEOUT_MS) as info:
                await page.evaluate("() => { location.hash = '#/pv/sites?r=' + Date.now(); }")
                await page.evaluate("() => { location.hash = '#/pv/sites'; }")
            retrieve = await (await info.value).json()
        except Exception as exc:
            raise RuntimeError(f"Chint site list never observed (visible-tab issue?): {exc}")
        site_list = (retrieve or {}).get("data") or []
        if not site_list:
            raise RuntimeError("Chint retrieve returned no sites")

        sites = []
        for st in site_list:
            sid = str(st.get("id") or "").strip()
            if not sid:
                continue
            devices = None
            try:
                async with page.expect_response(
                        lambda r: DEVICES in r.url, timeout=PER_SITE_TIMEOUT_MS) as info:
                    await page.evaluate(
                        "(id) => { location.hash = '#/pv/sites/siteDetail/' + id; }", sid)
                body = await (await info.value).json()
                devices = (body or {}).get("data")
            except Exception as exc:
                log.warning("Chint site %s devices not observed: %s", sid, exc)
            sites.append(self._site(st, devices))

        # be a good citizen: return to the list
        try:
            await page.evaluate("() => { location.hash = '#/pv/sites'; }")
        except Exception:
            pass

        sites = [s for s in sites if s]
        if not sites:
            raise RuntimeError("Chint: no sites assembled")
        return ScrapeResult(
            requests=[CaptureRequest(
                path="/v1/array-owners/inverter-capture",
                body={"provider": "chint", "sites": sites},
                note=f"chint {len(sites)} site(s)")],
            summary=f"{len(sites)} site(s)",
        )

    def _site(self, st: dict, devices: dict | None) -> dict:
        sid = str(st.get("id"))
        inverters = self._inverters(devices)
        live_w = None
        if devices:
            live_w = _power_to_w(devices.get("currentPowerWithUnit"))
        if live_w is None:
            live_w = _num(st.get("currentPower"))
        energy = round(sum(i["energy_today_kwh"] for i in inverters
                           if i["energy_today_kwh"]), 3) if inverters else None
        return {
            "site_id": sid,
            "name": st.get("siteName") or f"Chint site {sid}",
            "peak_power_kw": _kw_from_str(st.get("installedCapacity")),
            "inverter_count": len(inverters) or None,
            "energy_today_kwh": energy,
            "current_power_w": live_w,
            "error_count_today": sum(1 for i in inverters if i["status"] == "fault"),
            "status": "producing" if (live_w or 0) > 0 else "idle",
            "daily": self._week_trend(st),
            "inverters": inverters,
        }

    @staticmethod
    def _inverters(devices: dict | None) -> list[dict]:
        if not devices:
            return []
        out = []
        for gw in (devices.get("gwDevices") or []):
            for dvc in (gw.get("commDevices") or []):
                if not _is_inverter_device(dvc, gw):
                    continue
                serial = str(dvc.get("sn") or dvc.get("assetAlias") or dvc.get("id") or "").strip()
                if not serial:
                    continue
                pw = _num(dvc.get("currentPower"))
                status = _map_status(dvc.get("statusName"), pw)
                # 0-hold: omit an unexplained 0 W so the backend keeps the last good value.
                if pw == 0 and status not in ("offline", "fault"):
                    pw = None
                out.append({
                    "serial": serial,
                    "name": str(dvc.get("assetAlias") or dvc.get("sn") or serial),
                    "model": dvc.get("model") or None,
                    "nameplate_kw": None,
                    "energy_today_kwh": _num(dvc.get("eToday")),
                    "current_power_w": pw,
                    "status": status,
                })
        return out

    @staticmethod
    def _week_trend(st: dict) -> list[dict]:
        out = []
        for pt in (st.get("weekETrend") or []):
            name, val = str(pt.get("name") or ""), _num(pt.get("value"))
            if len(name) == 8 and val is not None:    # 'YYYYMMDD'
                out.append({"date": f"{name[:4]}-{name[4:6]}-{name[6:8]}", "kwh": val})
        return out


# A Chint gateway hangs several device KINDS off itself: the inverters (what we
# want) but ALSO the data-logger/collector itself, environmental "detectors",
# revenue meters, and string/combiner monitors. Only the inverters produce — the
# rest never make power, so if one slips into the inverter list it reads "quiet"
# forever and pollutes the unit count, peer analysis, and the alert sweep (it gets
# flagged as a dead inverter). Bruce's Londonderry 186 is the case that forced this:
# its FlexOM FG4C logger (hex serial 00009e021902bb00, no model, no power) was
# slipping in via the `assetType == 2` fallback. Doc: docs/knowledge/
# chint-portal-api-contract.md — "1 gateway (assetType 1) → 4 inverters"; the gateway
# is the "detector" Ford flagged. Recognize an inverter POSITIVELY and reject every
# known non-inverter kind, robust to whichever field the vendor (mis)populates.
_CHINT_NON_INVERTER_RE = re.compile(
    r"gateway|collector|logger|detector|meter|sensor|environment|weather|combiner"
    r"|module|dongle|\bdtu\b|\bemu\b", re.I)


def _is_inverter_device(dvc: dict, gw: dict | None = None) -> bool:
    if not isinstance(dvc, dict):
        return False
    type_name = str(dvc.get("assetTypeName") or "").strip()
    # 1) Explicit non-inverter kind by name — beats any assetType fallback below.
    if _CHINT_NON_INVERTER_RE.search(type_name):
        return False
    # 2) Chint asset-type ints (verified in the portal contract): 1 = Gateway.
    if dvc.get("assetType") == 1:
        return False
    # 3) A logger sometimes echoes itself as a commDevice under its OWN gateway — a
    #    commDevice whose serial equals the parent gateway's serial IS the gateway.
    sn = str(dvc.get("sn") or "").strip()
    gw_sn = str((gw or {}).get("sn") or (gw or {}).get("deviceSn")
                or (gw or {}).get("gatewaySn") or "").strip()
    if sn and gw_sn and sn == gw_sn:
        return False
    # 4) Positive identification by name always wins.
    if type_name.lower() == "inverter":
        return True
    # 5) Structural reject BEFORE the assetType==2 fallback: a logger under a
    #    DIFFERENT gateway (multi-gateway sites) arrives typeless AND modelless
    #    with a 16-char HEX serial — rule 3 can't catch it and the fallback was
    #    admitting it (Londonderry grew 0000e7be1902c000 / 000053571e02ca00 /
    #    00005cad1f022a00 as phantom dead "inverters"). Real CPS inverter
    #    serials are all digits; a model-less hex serial is never an inverter.
    if not dvc.get("model") and re.fullmatch(r"[0-9a-f]{12,20}", sn, re.I) \
            and re.search(r"[a-f]", sn, re.I):
        return False
    if dvc.get("assetType") == 2:
        return True
    return False


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _kw_from_str(v):
    n = _num(v)
    return n if n is not None else None


def _power_to_w(s):
    """Parse Chint's unit-suffixed live power string, e.g. '72.7 KW' → 72700."""
    if not s:
        return None
    m = re.match(r"\s*([\d.]+)\s*([kKmM]?)[wW]", str(s))
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    return val * (1_000_000 if unit == "m" else 1000 if unit == "k" else 1)


def _map_status(status_name, power_w):
    s = (status_name or "").lower()
    if "fault" in s or "error" in s or "alarm" in s:
        return "fault"
    if "offline" in s or "off-line" in s or "disconnect" in s:
        return "offline"
    if (power_w or 0) > 0:
        return "producing"
    return "idle"
