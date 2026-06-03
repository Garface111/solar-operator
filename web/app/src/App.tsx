import { useCallback, useEffect, useState } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useNavigate,
} from "react-router-dom";
import Login from "./screens/Login";
import Dashboard from "./screens/Dashboard";
import { Spinner } from "./ui/Spinner";
import {
  getSession,
  setSession,
  clearSession,
  verifyLoginToken,
  UNAUTHORIZED_EVENT,
} from "./lib/api";

// basename matches Vite's `base` and the FastAPI SPAStaticFiles mount at /app/.
export default function App() {
  return (
    <BrowserRouter basename="/app">
      <AuthGate />
    </BrowserRouter>
  );
}

type AuthState = "loading" | "authed" | "anon";

/**
 * Owns the session lifecycle:
 *  - On load, if the magic-link dropped a `?token=` in the URL, exchange it for
 *    a session via /v1/auth/verify, stash it, and clean the URL.
 *  - Otherwise trust an existing `so_session` in localStorage.
 *  - Any 401 anywhere in the app fires UNAUTHORIZED_EVENT → drop to login.
 */
function AuthGate() {
  const navigate = useNavigate();
  const [state, setState] = useState<AuthState>("loading");

  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams(window.location.search);
    // The magic link carries a one-time LOGIN token (param name `token`, or
    // `session` for forward-compat with the spec's wording) that we exchange
    // for a real session token here.
    const loginToken = params.get("token") ?? params.get("session");

    async function boot() {
      if (loginToken) {
        try {
          const session = await verifyLoginToken(loginToken);
          if (cancelled) return;
          setSession(session);
        } catch {
          // Bad/expired link — fall through to whatever session we have.
        }
        // Strip the token from the URL so a refresh/back doesn't re-use it.
        const url = new URL(window.location.href);
        url.searchParams.delete("token");
        url.searchParams.delete("session");
        window.history.replaceState({}, "", url.toString());
      }
      if (cancelled) return;
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
      setState("anon");
      navigate("/login", { replace: true });
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [navigate]);

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
        path="/"
        element={
          authed ? (
            <Dashboard onSignOut={onSignOut} />
          ) : (
            <Navigate to="/login" replace />
          )
        }
      />
      <Route
        path="/clients/:clientId"
        element={
          authed ? (
            <Dashboard onSignOut={onSignOut} />
          ) : (
            <Navigate to="/login" replace />
          )
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
