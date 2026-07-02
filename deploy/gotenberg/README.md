# Gotenberg render service

The document-render backend for the pixel-perfect invoice repro path.

Gotenberg (`gotenberg/gotenberg:8`) wraps a headless LibreOffice behind an HTTP
API. The Solar Operator app POSTs a filled `.xlsx` to
`/forms/libreoffice/convert` and receives the invoice as a PDF that matches the
operator's own workbook to the pixel. See `api/billing/repro/render.py`
(`_render_gotenberg`).

- **Local / self-host:** `docker-compose.yml` here runs it on `:3000`.
  ```bash
  docker compose -f deploy/gotenberg/docker-compose.yml up -d
  export GOTENBERG_URL=http://localhost:3000   # for the app
  ```
- **Railway (prod):** deploy the same image as a separate service. Full steps:
  `docs/REPRO-PIXEL-RUNBOOK.md`.

The app auto-falls-back to the bundled `libreoffice-calc` (shipped via
`railpack.json`) if Gotenberg is unset or unreachable, so this service is an
optimization, not a hard dependency.
