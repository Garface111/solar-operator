import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// Must match api/account.py _validate_password_strength: 10+ chars,
// at least one letter, at least one digit. We mirror the rule client-side
// to surface errors instantly instead of waiting for the /complete 400.
const PASSWORD_MIN = 10;
function passwordError(pw: string): string | undefined {
  if (pw.length < PASSWORD_MIN)
    return `Use at least ${PASSWORD_MIN} characters`;
  if (!/[a-zA-Z]/.test(pw)) return "Include at least one letter";
  if (!/[0-9]/.test(pw)) return "Include at least one number";
  return undefined;
}

export const SO_OPERATOR_KEY = "so_operator";
// Stashed alongside operator info so Done.tsx can hand it to
// completeOnboarding. We keep it in sessionStorage (cleared on Done after
// /complete succeeds) — never in localStorage and never in plain text on
// the server until the bcrypt hash lands in tenants.password_hash.
export const SO_OPERATOR_PASSWORD_KEY = "so_operator_password";

export interface OperatorInfo {
  email: string;
  fullName: string;
  company: string;
}

export default function Info() {
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [password, setPassword] = useState("");
  const [touched, setTouched] = useState<{ name?: boolean; email?: boolean; company?: boolean; password?: boolean }>({});

  // Stripe sends operators back here with ?cancelled=1 if they bail on Checkout.
  const cancelled =
    new URLSearchParams(window.location.search).get("cancelled") === "1";

  const nameError =
    touched.name && fullName.trim().length < 2 ? "Enter your name" : undefined;
  const companyError =
    touched.company && company.trim().length < 2
      ? "Enter your business or organization name"
      : undefined;
  const emailError =
    touched.email && !EMAIL_RE.test(email.trim())
      ? "Enter a valid email address"
      : undefined;
  const pwError = touched.password ? passwordError(password) : undefined;
  const valid =
    fullName.trim().length >= 2 &&
    company.trim().length >= 2 &&
    EMAIL_RE.test(email.trim()) &&
    !passwordError(password);

  function handleSubmit() {
    setTouched({ name: true, email: true, company: true, password: true });
    if (!valid) return;
    const info: OperatorInfo = {
      email: email.trim(),
      fullName: fullName.trim(),
      company: company.trim(),
    };
    // Store in sessionStorage so Plan can read it after client setup.
    sessionStorage.setItem(SO_OPERATOR_KEY, JSON.stringify(info));
    // Password rides in sessionStorage to Done.tsx where it's posted to
    // /v1/onboarding/complete and bcrypt-hashed server-side. Cleared the
    // moment that POST succeeds — never persisted past the wizard.
    sessionStorage.setItem(SO_OPERATOR_PASSWORD_KEY, password);
    navigate("/client-setup");
  }

  return (
    <ScreenLayout current={1}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Tell us who you are.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          We&apos;ll use this for your account and report sender details.
        </p>

        {cancelled && (
          <div className="mt-6 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            Checkout was cancelled. No charge was made — you can try again below.
          </div>
        )}

        <div className="mt-8 space-y-5">
          <Input
            id="full_name"
            label="What should we call you?"
            placeholder="Bruce"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, name: true }))}
            error={nameError}
            autoComplete="name"
          />
          <Input
            id="company"
            label="Business or organization name"
            placeholder="Green Valley Solar LLC"
            value={company}
            onChange={(e) => setCompany(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, company: true }))}
            error={companyError}
            autoComplete="organization"
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
          <div>
            <Input
              id="password"
              label="Password"
              type="password"
              placeholder="At least 10 characters, one letter, one number"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onBlur={() => setTouched((t) => ({ ...t, password: true }))}
              error={pwError}
              autoComplete="new-password"
            />
            <p className="mt-1.5 text-[11px] leading-relaxed text-zinc-500">
              Sign in instantly from any browser — no email-link wait. We
              also email a magic link as a backup for the future.
            </p>
          </div>
        </div>

        <div className="mt-8 flex items-center justify-between">
          <Button variant="ghost" onClick={() => navigate("/welcome")}>
            ← Back
          </Button>
          <Button onClick={handleSubmit} disabled={!valid}>
            Continue →
          </Button>
        </div>

        <p className="mt-6 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3 text-xs leading-relaxed text-zinc-500">
          Next: add your clients and arrays. We&apos;ll save your card to start your 14-day free trial — no charge today.
        </p>
      </Card>
    </ScreenLayout>
  );
}
