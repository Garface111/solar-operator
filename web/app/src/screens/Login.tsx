import { useState, type FormEvent } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { requestLoginLink } from "../lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface LoginProps {
  /** Called once a session exists (unused here — the magic link drives auth). */
  onLogin: () => void;
}

export default function Login(_props: LoginProps) {
  const toast = useToast();
  const [email, setEmail] = useState("");
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);

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
            <button
              type="button"
              onClick={() => setSent(false)}
              className="mt-6 rounded text-sm font-medium text-primary-600 transition-colors hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
            >
              Use a different email
            </button>
          </div>
        ) : (
          <form onSubmit={handleSubmit}>
            <h1 className="text-xl font-semibold tracking-tight text-zinc-900">
              Sign in to your account
            </h1>
            <p className="mt-2 text-sm text-zinc-500">
              Enter your email and we&apos;ll send you a one-time sign-in link.
            </p>
            <div className="mt-6">
              <Input
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
