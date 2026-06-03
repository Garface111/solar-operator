import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { createCheckout, setToken } from "../lib/onboarding";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function Info() {
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [touched, setTouched] = useState<{ name?: boolean; email?: boolean }>({});
  const [submitting, setSubmitting] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  // Stripe sends operators back here with ?cancelled=1 if they bail on Checkout.
  const cancelled =
    new URLSearchParams(window.location.search).get("cancelled") === "1";

  const nameError =
    touched.name && fullName.trim().length < 2 ? "Enter your full name" : undefined;
  const emailError =
    touched.email && !EMAIL_RE.test(email.trim())
      ? "Enter a valid email address"
      : undefined;
  const valid = fullName.trim().length >= 2 && EMAIL_RE.test(email.trim());

  async function handleSubmit() {
    setTouched({ name: true, email: true });
    if (!valid || submitting) return;
    setSubmitting(true);
    setServerError(null);
    try {
      const { checkout_url, onboarding_token } = await createCheckout({
        full_name: fullName.trim(),
        email: email.trim(),
        company: company.trim() || undefined,
      });
      // Persist before redirect; Stripe's success_url carries it back too.
      setToken(onboarding_token);
      window.location.href = checkout_url;
    } catch (err) {
      setServerError(err instanceof Error ? err.message : "Something went wrong");
      setSubmitting(false);
    }
  }

  return (
    <ScreenLayout current={1}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Tell us who you are.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          We&apos;ll use this for your account and report sender details. Next
          stop is secure checkout.
        </p>

        {cancelled && (
          <div className="mt-6 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            Checkout was cancelled. No charge was made — you can try again below.
          </div>
        )}

        <div className="mt-8 space-y-5">
          <Input
            id="full_name"
            label="Full name"
            placeholder="Jane Operator"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, name: true }))}
            error={nameError}
            autoComplete="name"
          />
          <Input
            id="email"
            label="Email"
            type="email"
            placeholder="jane@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, email: true }))}
            error={emailError}
            autoComplete="email"
          />
          <Input
            id="company"
            label="Company (optional)"
            placeholder="Green Valley Solar LLC"
            value={company}
            onChange={(e) => setCompany(e.target.value)}
            autoComplete="organization"
          />
        </div>

        {serverError && (
          <p className="mt-4 text-sm text-red-600">{serverError}</p>
        )}

        <div className="mt-8 flex items-center justify-between">
          <Button variant="ghost" onClick={() => navigate("/")} disabled={submitting}>
            ← Back
          </Button>
          <Button onClick={handleSubmit} disabled={!valid || submitting}>
            {submitting ? "Redirecting…" : "Continue to checkout →"}
          </Button>
        </div>
      </Card>
    </ScreenLayout>
  );
}
