import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { requestLoginLink } from "../lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Friendly translation of a magic-link verify failure carried via ?error=. */
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
  /** Called once a session exists (unused here — the magic link drives auth). */
  onLogin: () => void;
}

export default function Login(_props: LoginProps) {
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  // Cooldown counter (seconds) after a resend — prevents hammering the API.
  const [resendCooldown, setResendCooldown] = useState(0);
  const emailRef = useRef<HTMLInputElement>(null);

  const linkError = loginErrorMessage(searchParams.get("error"));

  // After a failed magic-link verify, focus the email field so the operator can
  // immediately request a new link.
  useEffect(() => {
    if (linkError) emailRef.current?.focus();
  }, [linkError]);

  const valid = EMAIL_RE.test(email.trim());

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!valid || sending) return;
    setSending(true);
    try {
      await requestLoginLink(email.trim().toLowerCase());
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
      await requestLoginLink(email.trim().toLowerCase());
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

  return (
    <div className="mx-auto flex min-h-full max-w-md flex-col justify-center px-4 py-12">
      <div className="mb-8 text-center">
        <div className="text-xl font-semibold tracking-tight text-zinc-900">
          <span className="text-primary-600">Solar</span> Operator
        </div>
      </div>

      <Card active>
        {sent ? (
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
              It expires in 15 minutes. No password needed.
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
          <form onSubmit={handleSubmit}>
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
            <Button
              type="submit"
              disabled={!valid || sending}
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
        )}
      </Card>

      <p className="mt-6 text-center text-xs text-zinc-400">
        Need help? Email support@solaroperator.org
      </p>
    </div>
  );
}
