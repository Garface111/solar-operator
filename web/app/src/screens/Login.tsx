import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useSearchParams } from "react-router-dom";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { requestLoginLink, passwordLogin, setSession } from "../lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const LAST_METHOD_KEY = "so:auth:last_method";
const PERSIST_KEY = "so_persist_session";

type Method = "magic" | "password";

function loginErrorMessage(code: string | null): string | null {
  switch (code) {
    case "expired":
      return "Your sign-in link expired — request a fresh one below.";
    case "used":
      return "That sign-in link has already been used — request a fresh one below.";
    case "invalid":
      return "We couldn't verify your sign-in link — request a fresh one below.";
    default:
      return null;
  }
}

interface LoginProps {
  onLogin: () => void;
}

export default function Login({ onLogin }: LoginProps) {
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const linkError = loginErrorMessage(searchParams.get("error"));

  const [method, setMethod] = useState<Method>(
    () => (localStorage.getItem(LAST_METHOD_KEY) as Method | null) ?? "magic",
  );
  const [email, setEmail] = useState("");
  const emailRef = useRef<HTMLInputElement>(null);

  // Shared sending state
  const [sending, setSending] = useState(false);

  // Magic-link specific
  const [sent, setSent] = useState(false);
  const [persist, setPersist] = useState(
    () => localStorage.getItem(PERSIST_KEY) !== "false",
  );
  const [resendCooldown, setResendCooldown] = useState(0);

  // Password specific
  const [password, setPassword] = useState("");
  const passwordRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (linkError) emailRef.current?.focus();
  }, [linkError]);

  function switchMethod(m: Method, prefillEmail?: string) {
    setMethod(m);
    localStorage.setItem(LAST_METHOD_KEY, m);
    if (prefillEmail !== undefined) setEmail(prefillEmail);
    setSent(false);
    setPassword("");
  }

  const emailValid = EMAIL_RE.test(email.trim());

  // ─── Magic link handlers ───────────────────────────────────────────────

  async function handleMagicSubmit(e: FormEvent) {
    e.preventDefault();
    if (!emailValid || sending) return;
    setSending(true);
    localStorage.setItem(PERSIST_KEY, String(persist));
    try {
      await requestLoginLink(email.trim().toLowerCase(), persist);
      setSent(true);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't send the link. Check your connection and try again.",
      );
    } finally {
      setSending(false);
    }
  }

  const startCooldown = useCallback(() => {
    setResendCooldown(60);
    const id = window.setInterval(() => {
      setResendCooldown((c) => {
        if (c <= 1) {
          window.clearInterval(id);
          return 0;
        }
        return c - 1;
      });
    }, 1000);
  }, []);

  async function handleResend() {
    if (resendCooldown > 0 || sending) return;
    setSending(true);
    try {
      await requestLoginLink(email.trim().toLowerCase(), persist);
      startCooldown();
      toast.show("New sign-in link sent.", "success");
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't resend the link. Try again in a moment.",
      );
    } finally {
      setSending(false);
    }
  }

  // ─── Password handler ──────────────────────────────────────────────────

  async function handlePasswordSubmit(e: FormEvent) {
    e.preventDefault();
    if (!emailValid || !password || sending) return;
    setSending(true);
    try {
      const token = await passwordLogin(email.trim().toLowerCase(), password);
      setSession(token);
      onLogin();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Sign-in failed. Try again.",
      );
    } finally {
      setSending(false);
    }
  }

  // ─── Render ────────────────────────────────────────────────────────────

  return (
    <div className="mx-auto flex min-h-full max-w-md flex-col justify-center px-4 py-12">
      <div className="mb-8 text-center">
        <div className="text-xl font-semibold tracking-tight text-zinc-900">
          <span className="text-primary-600">NEPOOL</span> Operator
        </div>
      </div>

      <Card active>
        {/* Tab bar */}
        <div className="mb-6 flex gap-1 rounded-lg bg-zinc-100 p-1">
          {(["magic", "password"] as Method[]).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => switchMethod(m)}
              className={[
                "flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                method === m
                  ? "bg-white text-zinc-900 shadow-sm"
                  : "text-zinc-500 hover:text-zinc-700",
              ].join(" ")}
            >
              {m === "magic" ? "Email me a link" : "Password"}
            </button>
          ))}
        </div>

        {/* Magic-link panel */}
        {method === "magic" && (
          sent ? (
            <div className="text-center">
              <div
                aria-hidden
                className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-primary-100 text-2xl text-primary-600"
              >
                ✉
              </div>
              <h1 className="mt-5 text-xl font-semibold tracking-tight text-zinc-900">
                Check your inbox
              </h1>
              <p className="mx-auto mt-2 max-w-sm text-sm text-zinc-500">
                We&apos;ve emailed a secure sign-in link to{" "}
                <span className="font-medium text-zinc-700">{email.trim()}</span>.
                It expires in 15 minutes.
              </p>
              <div className="mt-6 flex flex-col items-center gap-3">
                <button
                  type="button"
                  onClick={handleResend}
                  disabled={sending || resendCooldown > 0}
                  className="rounded text-sm font-medium text-primary-600 transition-colors hover:text-primary-700 disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                >
                  {sending
                    ? "Sending…"
                    : resendCooldown > 0
                      ? `Resend link (${resendCooldown}s)`
                      : "Resend link"}
                </button>
                <button
                  type="button"
                  onClick={() => setSent(false)}
                  className="rounded text-sm font-medium text-zinc-500 transition-colors hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                >
                  Use a different email
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={handleMagicSubmit}>
              <h1 className="text-xl font-semibold tracking-tight text-zinc-900">
                Sign in to your account
              </h1>
              <p className="mt-2 text-sm text-zinc-500">
                Enter your email and we&apos;ll send you a one-time sign-in link.
              </p>
              {linkError && (
                <div
                  role="alert"
                  className="mt-4 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
                >
                  {linkError}
                </div>
              )}
              <div className="mt-6">
                <Input
                  ref={emailRef}
                  id="login-email"
                  label="Email"
                  type="email"
                  autoComplete="email"
                  autoFocus
                  placeholder="you@example.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
              <label className="mt-4 flex cursor-pointer items-center gap-2.5 text-sm text-zinc-600">
                <input
                  type="checkbox"
                  checked={persist}
                  onChange={(e) => setPersist(e.target.checked)}
                  className="h-4 w-4 rounded border-zinc-300 text-primary-600 focus:ring-primary-500/40"
                />
                Trust this device for 30 days
              </label>
              <Button
                type="submit"
                disabled={!emailValid || sending}
                className="mt-6 w-full"
              >
                {sending ? (
                  <>
                    <Spinner />
                    Sending…
                  </>
                ) : (
                  "Email me a sign-in link"
                )}
              </Button>
            </form>
          )
        )}

        {/* Password panel */}
        {method === "password" && (
          <form onSubmit={handlePasswordSubmit}>
            <h1 className="text-xl font-semibold tracking-tight text-zinc-900">
              Sign in with password
            </h1>
            <p className="mt-2 text-sm text-zinc-500">
              Sign in directly without an email round-trip.
            </p>
            {linkError && (
              <div
                role="alert"
                className="mt-4 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
              >
                {linkError}
              </div>
            )}
            <div className="mt-6 flex flex-col gap-4">
              <Input
                ref={emailRef}
                id="pw-email"
                label="Email"
                type="email"
                autoComplete="email"
                autoFocus
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
              <Input
                ref={passwordRef}
                id="pw-password"
                label="Password"
                type="password"
                autoComplete="current-password"
                placeholder="••••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="mt-2 text-right">
              <button
                type="button"
                onClick={() => switchMethod("magic", email)}
                className="text-xs text-zinc-400 transition-colors hover:text-zinc-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
              >
                Forgot password?
              </button>
            </div>
            <Button
              type="submit"
              disabled={!emailValid || !password || sending}
              className="mt-5 w-full"
            >
              {sending ? (
                <>
                  <Spinner />
                  Signing in…
                </>
              ) : (
                "Sign in"
              )}
            </Button>
          </form>
        )}
      </Card>

      <p className="mt-6 text-center text-xs text-zinc-400">
        Need help? Email admin@solaroperator.org
      </p>
    </div>
  );
}
