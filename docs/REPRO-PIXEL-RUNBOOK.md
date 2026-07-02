# Pixel-Perfect Invoice Repro - Go-Live Runbook

Turn on the "to-the-pixel" invoice repro path in prod. The CODE is complete and
wired end-to-end behind a flag; this runbook is the exact operator steps to
provision the render backend, flip the flag, and verify. **Nothing here changes
until a human runs it** - the app ships dark by default.

## What this does

The repro path fills the operator's OWN workbook for the billing period (already
pixel-exact - it IS their file) and renders it to PDF with a headless office
engine, so what the offtaker receives is byte-for-byte their format. It is
gated so it can never mis-send: it only attaches the pixel PDF when a
deterministic numeric guard confirms the expected Amount Due is printed on it;
otherwise it silently falls back to the standard invoice.

Two interchangeable backends, chosen by env (`api/billing/repro/render.py`):

| Backend | Trigger | Notes |
|---|---|---|
| **Gotenberg** (preferred) | `GOTENBERG_URL` set | Separate Railway service running `gotenberg/gotenberg:8`. Scales independently, no heavy binary in the app image. |
| **Bundled LibreOffice** (fallback) | `soffice`/`libreoffice` on PATH | Already shipped: `railpack.json` installs `libreoffice-calc`. Auto-detected when `SOFFICE_BIN` is unset. Used automatically if Gotenberg is unreachable. |

Because libreoffice-calc is already in the app image, the path works with **no
Gotenberg at all** once the flag is on. Gotenberg is the recommended prod
backend (keeps LibreOffice's memory spikes off the web dyno). Do BOTH for
resilience: Gotenberg primary, bundled soffice as the automatic fallback.

---

## Prerequisites

- Railway CLI authed to the Solar Operator project (`railway whoami`), or use the
  Railway web dashboard.
- Decide the backend: **Gotenberg service** (recommended) and/or the
  already-bundled LibreOffice (zero extra infra).

---

## Option A - Gotenberg service (recommended)

### A1. Add the Gotenberg service (Railway dashboard)

1. Open the Solar Operator project in Railway.
2. **New -> Service -> Docker Image**.
3. Image: `gotenberg/gotenberg:8`
4. Under the new service's **Settings**:
   - **Start command** (Custom Start Command):
     `gotenberg --api-timeout=120s`
   - Confirm the container **Target Port** is `3000` (Gotenberg's default).
   - It does NOT need a public domain - the app reaches it over the private
     network. Leave "Generate Domain" OFF.
5. Deploy the service. Wait for it to go healthy (Gotenberg logs
   `http server started`).

CLI equivalent (from a shell that has run `unset RAILWAY_TOKEN` if a stale token
is in the profile):

```bash
railway add --service gotenberg --image gotenberg/gotenberg:8
# then set the start command + port in the dashboard (CLI can't set those yet)
```

### A2. Point the app at it

Gotenberg is reachable from the web service over Railway's private network at
`http://<gotenberg-service-name>.railway.internal:3000`. On the **web** service,
set:

```
GOTENBERG_URL = http://gotenberg.railway.internal:3000
```

(Replace `gotenberg` with the exact service name if you named it differently.)

Dashboard: web service -> **Variables** -> New Variable. Or CLI:

```bash
railway variables --service web --set "GOTENBERG_URL=http://gotenberg.railway.internal:3000"
```

> `.railway.internal` DNS resolves ONLY inside the Railway container network -
> you cannot curl it from your laptop. Verify from inside the web container
> (step C).

---

## Option B - Bundled LibreOffice only (no new service)

Already provisioned: `railpack.json` installs `libreoffice-calc`, and
`render.py` auto-detects `soffice` on PATH. **No env var needed** - just the
feature flag (next section). This is the zero-infra path; use it if you don't
want to run a second service. (You can still add Gotenberg later; the code
prefers Gotenberg when `GOTENBERG_URL` is set and falls back to soffice if it's
down.)

---

## Flip the flag (both options)

On the **web** service set:

```
REPRO_ENABLED = true
```

```bash
railway variables --service web --set "REPRO_ENABLED=true"
```

Setting a variable triggers a redeploy. Optional tuning vars (defaults are
fine):

| Var | Default | Meaning |
|---|---|---|
| `REPRO_RENDER_TIMEOUT_S` | `120` | Per-render timeout (seconds). |
| `SOFFICE_BIN` | auto-detect | Override the LibreOffice binary path. Leave unset. |

---

## Verify (do NOT skip - "done" = watched it work)

Run these READ-ONLY checks from inside the web container:
`railway ssh --service web` (after `unset RAILWAY_TOKEN`), then `python -`.

**1. Flag + backend are live:**

```python
from api.billing.repro import repro_enabled
from api.billing.repro import render as R
print("repro_enabled:", repro_enabled())          # expect True
print("backend:", R.active_backend())              # expect "gotenberg" (or "soffice")
print("renderer_available:", R.renderer_available())  # expect True
```

**2. Gotenberg is reachable + actually renders (Option A):**

```python
import io
from openpyxl import Workbook
from api.billing.repro import render as R
wb = Workbook(); ws = wb.active
ws.append(["Month","kWh","Amount Due"]); ws.append(["May",1000,182.34])
buf = io.BytesIO(); wb.save(buf)
pdf = R.render_xlsx_to_pdf(buf.getvalue())
print("PDF bytes:", len(pdf), "starts %PDF:", pdf[:4] == b"%PDF")  # expect a real length + True
```

If this raises `RenderError`, Gotenberg is misconfigured (check `GOTENBERG_URL`
and that the service is healthy). If it raises `RenderUnavailable`, neither
backend is configured (check the flag/vars).

**3. End-to-end on a real subscription (safe - renders only, sends nothing):**
pick a workbook subscription id and render its current-period invoice PDF in
memory, confirming the numeric guard passes:

```python
from api.billing.repro.pipeline import reproduce_for_subscription
# sub = <load a subscription with a stored source_workbook>
res = reproduce_for_subscription(sub, verify=False)
print("ok:", res.ok, "backend:", res.backend, "pdf?", bool(res.pdf))
# ok=True  -> pixel PDF will be attached on the next real send
# ok=False -> falls back to standard invoice (safe; investigate the column map)
```

**4. First real send:** trigger one real offtaker send (or wait for the next
scheduled run) and confirm the delivered attachment is the pixel PDF, not the
standard invoice. Look for the log line
`repro: pixel-perfect invoice PDF for sub <id> via gotenberg (verified ...)`.

---

## Rollback

Instant and safe - set `REPRO_ENABLED=false` (or delete the var) on the web
service. The next deploy reverts to the standard invoice path; no data
migration involved. You can leave the Gotenberg service running (idle) or delete
it.

---

## Notes / gotchas

- **Fail-closed by design:** with `REPRO_ENABLED=true` but no renderer, or an
  unverifiable render, the app falls back to the standard invoice - it never
  sends a blank or unverified PDF. Turning the flag on cannot break sends.
- **Private DNS only:** `*.railway.internal` won't resolve from your laptop;
  verify from inside the container.
- **Memory:** LibreOffice conversions spike RAM. On the bundled-only path this
  hits the web dyno; the Gotenberg service isolates it. Prefer Gotenberg if you
  see the web service OOM under load.
- **Live end-to-end (a rendered PDF from the real service) is the ONLY thing the
  unit tests can't cover** - the mock-backed test
  (`tests/test_repro_render_gotenberg.py`) proves the client posts the right
  payload and handles the PDF/error bytes, but a real Gotenberg round-trip must
  be verified here (steps 2-4).
