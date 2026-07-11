"""Cloud Capture — the server-side headless-browser harvesting engine.

The second capture path for Array Operator, alongside the browser extension.
Where the extension keeps the customer's portal passwords client-side and drives
login inside the operator's own browser (so data only flows while a tab is open),
Cloud Capture stores the password server-side (opt-in, encrypted at rest) and
drives login from a pool of headless Chromium browsers running in the cloud —
24/7, no tab required. The customer just gives us their logins once.

Why a real browser and not a server-side HTTP pull: the utility portals (GMP and
the ~530 SmartHub/NISC co-ops) have no public API and their data endpoints are
cookie-bound — a captured cookie cannot be replayed headlessly (they 403). A real
headless browser that performs the *human* login flow re-establishes the session
the legitimate way, executes the SPA's JS, and carries a real browser fingerprint,
which is exactly what defeats the ceiling that killed the old raw-HTTP approach.

Layout:
  config.py       — env-driven flags (global kill-switch defaults OFF).
  credentials.py  — the server-side vault: decrypt creds / persist session state.
  login.py        — generic username/password login routine (port of the
                    extension's soFillLoginForm + findBtn heuristics).
  stealth.py      — headless fingerprint hardening + context options.
  engine.py       — BrowserFarm: per-job context lifecycle + orchestration.
  scheduler.py    — enumerates due work from capture-debt and runs a tick.
  ingest_bridge.py— normalize scrape output into the existing capture writes.
  vendors/        — one module per portal family (login URL, is_logged_in, scrape).

Entrypoint: harvester_main.py at the repo root (own Railway service / Docker image).
"""
