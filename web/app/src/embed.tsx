/**
 * embed.tsx — "Generation reports" embed entry (THE FOLD).
 *
 * Builds the NEPOOL Operator screens into a chrome-less module that Array
 * Operator's vanilla SPA mounts inside its Invoices tab (third #rbGenTabs
 * pill). No TabBar, no gates, no product bounce — the AO shell owns auth,
 * navigation, and billing chrome. Session is the shared `so_session`
 * localStorage key; all API calls stay same-origin /v1/*.
 *
 * Built by `npm run build:embed` (vite.embed.config.ts) into dist-embed/
 * as a self-contained IIFE + one CSS file whose every selector is scoped
 * under #so-genrep so nothing leaks into the host page (and host element
 * rules stay out by specificity).
 *
 * Public API:  window.NepoolGenReports.mount(host) -> unmount()
 */
import { StrictMode, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  MemoryRouter,
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
} from "react-router-dom";
import { ToastProvider } from "./ui/Toast";
import { Spinner } from "./ui/Spinner";
import ClientsTab from "./screens/ClientsTab";
import NepoolReportsTab from "./screens/NepoolReportsTab";
import { lazyWithRetry } from "./lib/lazyWithRetry";
import type { DashboardContext } from "./screens/DashboardLayout";
import {
  type Account,
  UNAUTHORIZED_EVENT,
  getAccount,
  getSession,
} from "./lib/api";
import "./index.css";
import "./embed-sky.css"; // Sky material layer (THE FOLD Phase 3) - AFTER index.css so it wins order ties

const VerifyAccuracy = lazyWithRetry(() => import("./screens/VerifyAccuracy"));

function SectionSpinner() {
  return (
    <div className="flex min-h-[30vh] items-center justify-center text-zinc-400">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

/** Internal section nav — Clients is the landing (the roster you work from,
 *  matching the standalone app's default), Reports is one tab over. */
const SECTIONS = [
  { to: "/clients", label: "Clients" },
  { to: "/reports", label: "Reports" },
] as const;

/**
 * EmbedShell replaces DashboardLayout: loads the account once, hands the same
 * DashboardContext shape to the screens via <Outlet context>, and — crucially
 * — does NOT bounce array_operator accounts (that gate belongs to the
 * standalone /accounts SPA, not the fold).
 */
function EmbedShell() {
  const [account, setAccount] = useState<Account | null>(null);
  const [failed, setFailed] = useState(false);
  const [loadKey, setLoadKey] = useState(0);

  const retryLoad = useCallback(() => {
    setFailed(false);
    setLoadKey((k) => k + 1);
  }, []);

  const patchAccount = useCallback((patch: Partial<Account>) => {
    setAccount((a) => (a ? { ...a, ...patch } : a));
  }, []);

  useEffect(() => {
    let cancelled = false;
    getAccount()
      .then((a) => {
        if (!cancelled) setAccount(a);
      })
      .catch((err) => {
        if (err?.name === "UnauthorizedError") return; // handled globally below
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [loadKey]);

  const ctx = useMemo<DashboardContext>(
    () => ({ account, failed, patchAccount, retryLoad }),
    [account, failed, patchAccount, retryLoad],
  );

  return (
    <div className="min-h-[24rem] font-sans text-zinc-900">
      <nav
        role="tablist"
        aria-label="Generation reports sections"
        className="mb-4 inline-flex items-center gap-1 rounded-full border border-cream-border bg-white/70 p-1"
      >
        {SECTIONS.map((s) => (
          <NavLink
            key={s.to}
            to={s.to}
            role="tab"
            className={({ isActive }) =>
              "rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors " +
              (isActive
                ? "bg-primary-700 text-white shadow-sm"
                : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900")
            }
          >
            {s.label}
          </NavLink>
        ))}
      </nav>
      <Outlet context={ctx} />
    </div>
  );
}

/** Shown when the shared session dies mid-use — the AO shell owns sign-in. */
function SessionExpired() {
  return (
    <div className="flex min-h-[24rem] flex-col items-center justify-center gap-2 text-center">
      <p className="text-base font-semibold text-zinc-800">Your session expired.</p>
      <p className="text-sm text-zinc-500">
        Sign back in from the main page to keep working on generation reports.
      </p>
    </div>
  );
}

function EmbedApp() {
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    const onUnauthorized = () => setExpired(true);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (expired || !getSession()) return <SessionExpired />;

  return (
    <MemoryRouter initialEntries={["/clients"]}>
      <Routes>
        <Route element={<EmbedShell />}>
          <Route index element={<Navigate to="/clients" replace />} />
          <Route path="/reports" element={<NepoolReportsTab />} />
          <Route path="/clients" element={<ClientsTab />} />
          <Route path="/clients/:clientId" element={<ClientsTab />} />
          <Route
            path="/verify/:clientId"
            element={
              <Suspense fallback={<SectionSpinner />}>
                <VerifyAccuracy />
              </Suspense>
            }
          />
          <Route path="*" element={<Navigate to="/clients" replace />} />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

// ─── public mount API ────────────────────────────────────────────────────────

const ROOT_ID = "so-genrep"; // every built CSS selector is scoped under this id

function mount(host: HTMLElement): () => void {
  // One instance at a time — the scoped stylesheet keys on a single id.
  const prior = document.getElementById(ROOT_ID);
  if (prior) prior.remove();

  const el = document.createElement("div");
  el.id = ROOT_ID;
  host.appendChild(el);

  const root = createRoot(el);
  root.render(
    <StrictMode>
      <ToastProvider>
        <EmbedApp />
      </ToastProvider>
    </StrictMode>,
  );

  return () => {
    root.unmount();
    el.remove();
  };
}

declare global {
  interface Window {
    NepoolGenReports?: { mount: typeof mount };
    __soEventsBase?: string;
    __soGenrepEmbed?: boolean;
  }
}

// Signals the shared screens they're mounted inside Array Operator's Invoices
// tab (not the standalone /accounts SPA). The Clients roster reads this to hide
// retired (inactive) clients — after the fold a folded tenant can carry many
// inactive capture-artifact clients that would otherwise clutter the view.
window.__soGenrepEmbed = true;

// SSE must skip the Netlify /v1 proxy (it buffers event-streams ~21s to first
// byte — measured live 2026-07-16). Point SSE, and only SSE, at the Railway
// origin; AO's CSP connect-src and the backend's CORS already allow it.
window.__soEventsBase = "https://web-production-49c83.up.railway.app";

window.NepoolGenReports = { mount };
