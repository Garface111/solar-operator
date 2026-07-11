"""Cloud Capture harvester — entrypoint for the standalone Railway service / Docker image.

Modes:
  (default)                      run the scheduler loop forever (the service).
  --once                         run a single tick and exit.
  --tenant T --provider P
      [--username U]             harvest exactly one credential and exit (verify).
  --selftest [--provider gmp|<coop-host>]
                                 credential-FREE probe: open the live login page and
                                 confirm the generic login routine locates the
                                 username / password / submit fields. Proves the
                                 mechanism against the real DOM without ever
                                 submitting a password (no account-lockout risk).

Env: see api/harvester/config.py. The global switch CLOUD_CAPTURE_ENABLED gates
the loop and --once; --selftest and an explicit --tenant run ignore the switch so
you can verify before flipping anything on.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _run_once():
    from api.harvester.engine import BrowserFarm
    from api.harvester.scheduler import run_tick
    async with BrowserFarm() as farm:
        results = await run_tick(farm)
        for r in results:
            print("  ", r)


async def _run_one(tenant: str, provider: str, username: str | None):
    from sqlalchemy import select
    from api.db import SessionLocal
    from api.models import PortalCredential
    from api.harvester.engine import BrowserFarm

    # Resolve the username_lc (allow omitting it when a tenant/provider has one login).
    with SessionLocal() as db:
        q = select(PortalCredential).where(
            PortalCredential.tenant_id == tenant,
            PortalCredential.provider == provider,
        )
        if username:
            q = q.where(PortalCredential.username_lc == username.lower())
        rows = db.execute(q).scalars().all()
    if not rows:
        print(f"no credential for tenant={tenant} provider={provider} username={username}")
        return
    if len(rows) > 1 and not username:
        print(f"{len(rows)} logins for {tenant}/{provider} — pass --username to pick one")
        return
    ulc = rows[0].username_lc
    async with BrowserFarm() as farm:
        outcome = await farm.harvest(tenant, provider, ulc)
        print("OUTCOME:", outcome, "| detail:", outcome.detail)


async def _selftest(provider: str):
    """Open the live login page and report which of username/password/submit the
    generic routine finds. No credentials, no submit."""
    from playwright.async_api import async_playwright
    from api.harvester import stealth, login as L

    INV = {"fronius": "https://www.solarweb.com/",
           "sma": "https://ennexos.sunnyportal.com/",
           "chint": "https://monitor.chintpowersystems.com/"}
    if provider == "gmp":
        url, hintkey = "https://greenmountainpower.com/account/login/", "gmp"
    elif provider in INV:
        url, hintkey = INV[provider], provider
    else:
        host = provider if "." in provider else "vermontelectric.smarthub.coop"
        url, hintkey = f"https://{host}/", "smarthub"
    hint = L.HINTS.get(hintkey)

    print(f"selftest: {url} (hint={hintkey})")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=stealth.launch_args())
        ctx = await browser.new_context(**stealth.context_options())
        await stealth.apply(ctx)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            # Give SPA logins a moment to render their form.
            await asyncio.sleep(3)
            user = await L._find_user(page, hint)
            pw_el = await L._find_pass(page, hint)
            # SSO-redirect portals (Fronius/SMA) show a landing "Login" button
            # first — click it to reach the real form, mirroring perform_login.
            if not user and not pw_el:
                if await L._click_login_entry(page):
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    print("  (clicked login-entry →", page.url, ")")
                    user = await L._find_user(page, hint)
                    pw_el = await L._find_pass(page, hint)
            btn = await L._find_btn(page, hint)
            async def describe(el):
                if not el:
                    return "NOT FOUND"
                try:
                    tag = await el.evaluate("e => e.tagName.toLowerCase()")
                    idv = await el.evaluate("e => e.id || e.name || e.getAttribute('formcontrolname') || ''")
                    txt = await el.evaluate("e => (e.textContent||e.value||'').trim().slice(0,30)")
                    return f"<{tag}> id/name='{idv}' text='{txt}'"
                except Exception:
                    return "found (undescribable)"
            print("  final url :", page.url)
            print("  username  :", await describe(user))
            print("  password  :", await describe(pw_el))
            print("  submit    :", await describe(btn))
            ok = bool(user and btn)          # a login page has at least a user field + a submit
            print("  RESULT    :", "PASS — login form detected" if ok else "PARTIAL/FAIL")
        finally:
            await ctx.close()
            await browser.close()


def main(argv=None):
    _setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--tenant")
    ap.add_argument("--provider")
    ap.add_argument("--username")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        asyncio.run(_selftest(args.provider or "gmp"))
        return
    if args.tenant and args.provider:
        asyncio.run(_run_one(args.tenant, args.provider, args.username))
        return
    if args.once:
        asyncio.run(_run_once())
        return

    # Default: the service loop.
    from api.harvester import config
    if not config.enabled():
        print("CLOUD_CAPTURE_ENABLED is not set — the harvester loop is a no-op. "
              "Set it to run, or use --selftest / --once / --tenant to verify.")
        return
    from api.harvester.scheduler import run_forever
    asyncio.run(run_forever())


if __name__ == "__main__":
    main(sys.argv[1:])
