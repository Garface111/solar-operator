# Solar Operator — operator SPA

Vite + React + TypeScript single-page app for the `/accounts/*` operator dashboard.

## Commands

- `npm run dev` — local dev server (default http://localhost:5173)
- `npm run build` — typecheck + production build into `dist/`
- `npm test` — vitest

## Environment variables

| Var | Default | Purpose |
| --- | --- | --- |
| `VITE_API_PROXY` | `https://solaroperator.org` | Upstream API proxied during `npm run dev` (see `vite.config.ts`). |
| `VITE_MIND_BASE` | `http://localhost:8001` | Base URL for the OCICBB ("Talk to OCICBB" Mind) chat backend. The Mind button posts to `${VITE_MIND_BASE}/v1/chat`. Unset → local dev default. |

### Mind button (Talk to OCICBB)

The floating "Talk to OCICBB" button is a private dogfood feature: it renders
only for operators in `MIND_BUTTON_ALLOWED_EMAILS` (see
`src/components/MindButton.tsx`). It streams the Mind's SSE response from
`${VITE_MIND_BASE}/v1/chat`. Session continuity is a per-browser UUID stored in
`localStorage` under `mind-session-id`.

To dogfood against a local Mind, run the Mind on `localhost:8001` (or point
`VITE_MIND_BASE` elsewhere) and sign in as an allow-listed operator.

> **CORS:** the Mind backend must allow the SPA origin (e.g.
> `http://localhost:5173`) on `/v1/chat`. If requests fail CORS, update the
> Mind/master-control backend — not this repo.
