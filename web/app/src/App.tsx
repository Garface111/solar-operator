import { Suspense, useCallback, useEffect, useState } from "react";
import { lazyWithRetry } from "./lib/lazyWithRetry";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useNavigate,
} from "react-router-dom";
import Login from "./screens/Login";
import DashboardLayout from "./screens/DashboardLayout";
import ClientsTab from "./screens/ClientsTab";
import { Spinner } from "./ui/Spinner";

const CaptureTimeline = lazyWithRetry(() => import("./components/dev/CaptureTimeline"));

const VerifyAccuracy = lazyWithRetry(() => import("./screens/VerifyAccuracy"));

const AccountTab = lazyWithRetry(() => import("./screens/AccountTab"));
const ReportsTab = lazyWithRetry(() => import("./screens/ReportsTab"));
import { useToast } from "./ui/Toast";
import {
  getSession,
  setSession,
  clearSession,
  verifyLoginToken,
  UNAUTHORIZED_EVENT,
} from "./lib/api";

function TabSpinner() {
  return (
    <div className="flex min-h-[40vh] items-center justify-center text-zinc-400">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

// basename matches Vite's `base` and the public URL nepooloperator.com/accounts
// (Netlify proxies that to the FastAPI mount at /app/ on Railway).
export default function App() {
  return (
    <BrowserRouter basename="/accounts">
      <AuthGate />
    </BrowserRouter>
  );
}

type AuthState = "loading" | "authed" | "anon";

/** Map a /v1/auth/verify failure to a stable login-screen error code. The API
 *  returns specific reasons ("already used", "expired", "invalid"). */
function classifyLoginError(err: unknown): string {
  const msg = (err instanceof Error ? err.message : "").toLowerCase();
  if (msg.includes("already used")) return "used";
  if (msg.includes("expired")) return "expired";
  return "invalid";
}

/**
 * Owns the session lifecycle:
 *  - On load, if the magic-link dropped a `?token=` in the URL, exchange it for
 *    a session via /v1/auth/verify, stash it, and clean the URL.
 *  - Otherwise trust an existing `so_session` in localStorage.
 *  - Any 401 anywhere in the app fires UNAUTHORIZED_EVENT → drop to login.
 */
function AuthGate() {
  const navigate = useNavigate();
  const toast = useToast();
  const [state, setState] = useState<AuthState>("loading");

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);
    // The magic link carries a one-time LOGIN token (param name `token`, or
    // `session` for forward-compat with the spec's wording) that we exchange
    // for a real session token here.
    const loginToken = params.get("token") ?? params.get("session");

    async function boot() {
      let loginError: string | null = null;
      if (loginToken) {
        try {
          const session = await verifyLoginToken(loginToken);
          if (cancelled) return;
          setSession(session);
        } catch (err) {
          // Bad/expired/used link — carry the reason to the login screen so we
          // can tell the operator *why* instead of dropping them on a bare form.
          loginError = classifyLoginError(err);
        }
        // Strip the token from the URL so a refresh/back doesn't re-use it.
        const url = new URL(window.location.href);
        url.searchParams.delete("token");
        url.searchParams.delete("session");
        window.history.replaceState({}, "", url.toString());
      }
      if (cancelled) return;
      // A failed verify with no existing session → login, surfacing the reason.
      if (loginError && !getSession()) {
        setState("anon");
        navigate(`/login?error=${loginError}`, { replace: true });
        return;
      }
      setState(getSession() ? "authed" : "anon");
    }

    boot();
    return () => {
      cancelled = true;
    };
  }, []);

  const onLogin = useCallback(() => setState("authed"), []);
  const onSignOut = useCallback(() => {
    clearSession();
    setState("anon");
    navigate("/login", { replace: true });
  }, [navigate]);

  // Global 401 handler — clearSession() already ran inside the api client.
  useEffect(() => {
    function onUnauthorized() {
      toast.show("Your session expired — sign in again to continue.", "info");
      setState("anon");
      navigate("/login", { replace: true });
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [navigate, toast]);

  if (state === "loading") {
    return (
      <div className="flex min-h-full items-center justify-center text-zinc-400">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  const authed = state === "authed";

  return (
    <Routes>
      <Route
        path="/login"
        element={
          authed ? <Navigate to="/" replace /> : <Login onLogin={onLogin} />
        }
      />
      <Route
        element={
          authed ? (
            <DashboardLayout onSignOut={onSignOut} />
          ) : (
            <Navigate to="/login" replace />
          )
        }
      >
        {/* /accounts/ → Clients tab (the post-onboarding landing where the
            walkthrough anchors live). Account tab is one tab away. */}
        <Route index element={<Navigate to="/clients" replace />} />
        <Route
          path="/account"
          element={
            <Suspense fallback={<TabSpinner />}>
              <AccountTab />
            </Suspense>
          }
        />
        <Route path="/clients" element={<ClientsTab />} />
        {/* Arrays tab retired — keep the path alive so old bookmarks/deep
            links land on Clients instead of 404ing. */}
        <Route path="/arrays" element={<Navigate to="/clients" replace />} />
        {/* Deep link that auto-expands a single client. */}
        <Route path="/clients/:clientId" element={<ClientsTab />} />
        <Route
          path="/reports"
          element={
            <Suspense fallback={<TabSpinner />}>
              <ReportsTab />
            </Suspense>
          }
        />
        <Route path="/sandbox" element={<Navigate to="/clients" replace />} />
        <Route
          path="/verify/:clientId"
          element={
            <Suspense fallback={<TabSpinner />}>
              <VerifyAccuracy />
            </Suspense>
          }
        />
        <Route
          path="/dev/captures"
          element={
            <Suspense fallback={<TabSpinner />}>
              <CaptureTimeline />
            </Suspense>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
