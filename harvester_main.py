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
import os
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
    print("harvester_main: CLOUD_CAPTURE_ENABLED=1 — starting health server, seed, loop", flush=True)
    _start_health_server()
    _maybe_seed()
    from api.harvester.scheduler import run_forever
    asyncio.run(run_forever())


def _start_health_server():
    """Serve 200 on / and /health on $PORT in a daemon thread. The harvester runs
    its Dockerfile CMD (a browser loop, not an HTTP app), but this repo's shared
    railway.toml applies healthcheckPath=/health to every service — without a
    responder the harvester deploy never goes healthy. Cheap stdlib server; the
    loop runs alongside it."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(os.environ.get("PORT") or 8080)

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_a):
            pass

    def _run():
        try:
            HTTPServer(("0.0.0.0", port), _H).serve_forever()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _maybe_seed():
    """One-time in-container credential bootstrap — DISABLED by default (T2-8).

    Requires CC_SEED_ALLOW=1 PLUS CC_SEED_USERNAME + CC_SEED_PASSWORD. After a
    successful seed, operators must delete all CC_SEED_* vars immediately.
    Leaving a portal password in Railway env is forbidden long-term.
    """
    import logging
    log = logging.getLogger("harvester.seed")
    if (os.environ.get("CC_SEED_ALLOW") or "").strip().lower() not in ("1", "true", "yes", "on"):
        # If password is still present without allow flag, scream once — don't use it.
        if os.environ.get("CC_SEED_PASSWORD"):
            log.error(
                "CC_SEED_PASSWORD is set but CC_SEED_ALLOW!=1 — refusing to seed. "
                "Remove CC_SEED_PASSWORD from the service env now."
            )
        return
    user = os.environ.get("CC_SEED_USERNAME")
    pw = os.environ.get("CC_SEED_PASSWORD")
    if not (user and pw):
        return
    provider = (os.environ.get("CC_SEED_PROVIDER") or "").strip().lower()
    tenant = os.environ.get("CC_SEED_TENANT")
    array_like = os.environ.get("CC_SEED_ARRAY_LIKE")
    host = os.environ.get("CC_SEED_HOST")
    try:
        from sqlalchemy import select
        from api.db import SessionLocal
        from api.models import Array
        from api.harvester import credentials as cc
        if not cc.crypto_ready():
            log.warning("seed skipped — SO_CONFIG_KEY not set (cannot encrypt)")
            return
        with SessionLocal() as db:
            if not tenant and array_like:
                row = db.execute(
                    select(Array.tenant_id).where(Array.name.ilike(f"%{array_like}%")).limit(1)
                ).first()
                tenant = row[0] if row else None
            if not tenant:
                log.warning("seed skipped — could not resolve tenant")
                return
            cc.upsert_credential(db, tenant, provider, user, pw, login_host=host, enable=True)
            db.commit()
        log.info("seed: cloud-capture cred upserted (tenant=%s provider=%s) — "
                 "remove CC_SEED_* env now", tenant, provider)
    except Exception as exc:                          # never let a seed error kill the loop
        log.warning("seed failed (continuing): %s", type(exc).__name__)


if __name__ == "__main__":
    main(sys.argv[1:])
