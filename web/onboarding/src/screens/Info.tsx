import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export const SO_OPERATOR_KEY = "so_operator";

export interface OperatorInfo {
  email: string;
  fullName: string;
  company?: string;
}

export default function Info() {
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [touched, setTouched] = useState<{ name?: boolean; email?: boolean }>({});

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

  function handleSubmit() {
    setTouched({ name: true, email: true });
    if (!valid) return;
    const info: OperatorInfo = {
      email: email.trim(),
      fullName: fullName.trim(),
      company: company.trim() || undefined,
    };
    // Store in sessionStorage so Plan can read it after client setup.
    sessionStorage.setItem(SO_OPERATOR_KEY, JSON.stringify(info));
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
