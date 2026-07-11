"""BrowserFarm — the per-job browser lifecycle + orchestration.

One Playwright + one Chromium process; a FRESH BrowserContext per job (so tenant
sessions never bleed into each other and each loads its own persisted
storage_state). Concurrency is bounded by a semaphore — a stampede of logins
from one IP is the fastest way to get flagged.

DB-session discipline (the meltdown lesson, memory: energyagent-billing-pool-leak):
NEVER hold a transaction across the browser work. We open a SHORT session to read
the decrypted credential into memory, close it, do the slow login+scrape with NO
session held, then open a second SHORT session to persist session_state + health.
The plaintext password lives only in the in-memory Creds for the job's duration.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime

from sqlalchemy import select

from ..db import SessionLocal
from ..models import PortalCredential, now
from . import config, credentials, login, stealth
from .ingest_bridge import deliver
from .vendors import module_for

log = logging.getLogger("harvester.engine")


class HarvestOutcome:
    def __init__(self, provider, username_lc, status, rows=0, fresh=False, detail=""):
        self.provider = provider
        self.username_lc = username_lc
        self.status = status
        self.rows = rows
        self.fresh = fresh
        self.detail = detail

    def __repr__(self):
        return f"<Harvest {self.provider}/{self.username_lc} {self.status} rows={self.rows}>"


class BrowserFarm:
    def __init__(self):
        self._pw = None
        self._browser = None
        self._sem = asyncio.Semaphore(config.concurrency())

    async def __aenter__(self):
        # Imported lazily so the rest of the codebase (API service) never needs
        # Playwright installed — only the harvester image does.
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=config.headless(),
            args=stealth.launch_args(),
        )
        log.info("BrowserFarm up (headless=%s, concurrency=%d)",
                 config.headless(), config.concurrency())
        return self

    async def __aexit__(self, *exc):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ── load / persist (short DB sessions only) ────────────────────────────────
    @staticmethod
    def _load(tenant_id: str, provider: str, username_lc: str):
        """Read the credential + decrypt into an in-memory Creds. Short session."""
        with SessionLocal() as db:
            cred = db.execute(
                select(PortalCredential).where(
                    PortalCredential.tenant_id == tenant_id,
                    PortalCredential.provider == provider,
                    PortalCredential.username_lc == username_lc,
                )
            ).scalar_one_or_none()
            if cred is None:
                return None
            return credentials.load_creds(cred)

    @staticmethod
    def _persist(tenant_id, provider, username_lc, *, storage_state, ok, status,
                 started_at, fresh, rows, error, shot):
        """Persist session_state + health + audit row. Short session, one commit."""
        with SessionLocal() as db:
            cred = db.execute(
                select(PortalCredential).where(
                    PortalCredential.tenant_id == tenant_id,
                    PortalCredential.provider == provider,
                    PortalCredential.username_lc == username_lc,
                )
            ).scalar_one_or_none()
            if cred is None:
                return
            if storage_state is not None:
                credentials.save_session_state(db, cred, storage_state)
            credentials.record_health(
                db, cred, ok=ok, status=status, started_at=started_at,
                fresh_login=fresh, rows_written=rows, error=error,
                screenshot_ref=shot,
            )
            db.commit()

    # ── the job ────────────────────────────────────────────────────────────────
    async def harvest(self, tenant_id: str, provider: str, username_lc: str) -> HarvestOutcome:
        async with self._sem:
            # Jitter so many due logins don't fire in a synchronized burst.
            await asyncio.sleep(random.uniform(0, config.per_login_jitter_seconds()))
            return await self._harvest_inner(tenant_id, provider, username_lc)

    async def _harvest_inner(self, tenant_id, provider, username_lc) -> HarvestOutcome:
        started = now()
        creds = self._load(tenant_id, provider, username_lc)
        if creds is None or not creds.password:
            self._persist(tenant_id, provider, username_lc, storage_state=None,
                          ok=False, status="no_creds", started_at=started,
                          fresh=False, rows=0, error="no stored password", shot=None)
            return HarvestOutcome(provider, username_lc, "no_creds")

        vendor = module_for(provider)
        if vendor is None:
            self._persist(tenant_id, provider, username_lc, storage_state=None,
                          ok=False, status="skipped", started_at=started,
                          fresh=False, rows=0, error=f"no vendor module for {provider}",
                          shot=None)
            return HarvestOutcome(provider, username_lc, "skipped")

        context = page = None
        storage_state = None
        shot = None
        fresh = False
        try:
            context = await self._browser.new_context(
                **stealth.context_options(creds.session_state))
            await stealth.apply(context)
            context.set_default_timeout(config.action_timeout_ms())
            context.set_default_navigation_timeout(config.nav_timeout_ms())
            page = await context.new_page()

            url = await vendor.login_url(creds)
            await page.goto(url, wait_until="domcontentloaded")
            # Let the SPA render before probing auth state — otherwise a login
            # form that hasn't painted yet reads as "already logged in", login is
            # skipped, and the authenticated scrape then finds nothing.
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

            if not await vendor.is_logged_in(page):
                # Warm session missing/expired → do the human login flow.
                fresh = True
                outcome = await login.perform_login(
                    page, creds.username, creds.password, provider)
                # Poll for the authenticated view to land — SPA logins (Chint)
                # transition for a few seconds after submit before the dashboard
                # renders, so a single immediate check reads as still-logged-out.
                try:
                    await page.wait_for_load_state("networkidle", timeout=config.nav_timeout_ms())
                except Exception:
                    pass
                _logged = False
                for _ in range(6):
                    if await vendor.is_logged_in(page):
                        _logged = True
                        break
                    await asyncio.sleep(2)
                if not _logged:
                    shot = await self._screenshot(page, tenant_id, provider, "login")
                    storage_state = await context.storage_state()
                    self._persist(tenant_id, provider, username_lc,
                                  storage_state=storage_state, ok=False,
                                  status="login_failed", started_at=started,
                                  fresh=True, rows=0,
                                  error=f"login outcome={outcome}, not authenticated",
                                  shot=shot)
                    return HarvestOutcome(provider, username_lc, "login_failed",
                                          fresh=True, detail=outcome)

            result = await vendor.scrape(page, context, creds)
            storage_state = await context.storage_state()

            # Deliver through the existing capture endpoints (same as extension).
            rows = await deliver(tenant_id, result.requests)

            self._persist(tenant_id, provider, username_lc,
                          storage_state=storage_state, ok=True, status="ok",
                          started_at=started, fresh=fresh, rows=rows,
                          error=(result.summary or None), shot=None)
            return HarvestOutcome(provider, username_lc, "ok", rows=rows, fresh=fresh,
                                  detail=result.summary)

        except Exception as exc:                       # noqa: BLE001 — record & continue
            log.warning("harvest error %s/%s: %s", provider, tenant_id, exc)
            if page is not None:
                shot = await self._screenshot(page, tenant_id, provider, "error")
            try:
                if context is not None:
                    storage_state = await context.storage_state()
            except Exception:
                pass
            self._persist(tenant_id, provider, username_lc,
                          storage_state=storage_state, ok=False, status="scrape_failed",
                          started_at=started, fresh=fresh,
                          rows=0, error=str(exc)[:500], shot=shot)
            return HarvestOutcome(provider, username_lc, "scrape_failed", detail=str(exc)[:200])
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

    @staticmethod
    async def _screenshot(page, tenant_id, provider, tag) -> str | None:
        """Save a failure screenshot for debugging a broken selector/flow."""
        try:
            d = config.screenshot_dir()
            os.makedirs(d, exist_ok=True)
            # No timestamp fn available in some sandboxes; use a stable-ish name.
            ref = os.path.join(d, f"{provider}_{tenant_id}_{tag}.png")
            await page.screenshot(path=ref, full_page=False)
            return ref
        except Exception:
            return None
