import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Checkbox";
import { MarkdownDoc } from "../ui/MarkdownDoc";

const SERVICES = [
  "Auto-pull GMP bills",
  "NEPOOL-format Excel reports",
  "Email delivery to your clients",
  "Multi-client portal",
];

// Served from web/onboarding/public/ by Vite (and FastAPI in prod) under the
// app's base path, e.g. /onboarding/tos.md.
const BASE = import.meta.env.BASE_URL;

export default function Welcome() {
  const navigate = useNavigate();
  const [agreed, setAgreed] = useState(false);
  const [legalOpen, setLegalOpen] = useState(false);

  return (
    <ScreenLayout current={0}>
      <Card active>
        <h1 className="text-3xl font-semibold tracking-tight text-zinc-900">
          Quarterly solar reports, on autopilot.
        </h1>
        <p className="mt-3 text-base text-zinc-500">
          We pull your Green Mountain Power bills, build NEPOOL-format
          net-metering credit reports, and email them to your clients every
          quarter — so you never touch a spreadsheet again.
        </p>

        <div className="mt-8 rounded-xl border border-primary-200 bg-primary-50 p-5">
          <p className="text-sm font-semibold text-primary-800">
            $250 one-time setup · $45/array/month · cancel anytime
          </p>
          <p className="mt-1 text-xs text-primary-700">
            Billed monthly. Each array you manage is one billing unit — the
            count is set automatically as GMP captures come in.
          </p>
        </div>

        <ul className="mt-6 grid gap-2 sm:grid-cols-2">
          {SERVICES.map((s) => (
            <li key={s} className="flex items-center gap-2 text-sm text-zinc-700">
              <span
                aria-hidden
                className="flex h-5 w-5 items-center justify-center rounded-full bg-primary-100 text-primary-700"
              >
                ✓
              </span>
              {s}
            </li>
          ))}
        </ul>

        <div className="mt-8 flex gap-4 rounded-xl border border-primary-100 bg-primary-50/60 px-4 py-3 text-sm text-primary-800">
          <span aria-hidden className="mt-0.5 text-primary-500">🔒</span>
          <span>
            Before continuing, please read our{" "}
            <a
              href={`${BASE}privacy.md`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-semibold underline underline-offset-2 hover:text-primary-900"
            >
              Privacy Policy
            </a>{" "}
            and{" "}
            <a
              href={`${BASE}tos.md`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-semibold underline underline-offset-2 hover:text-primary-900"
            >
              Terms of Service
            </a>
            . Key points: we never sell your data, you can delete your account
            anytime, and we charge per array.
          </span>
        </div>

        <div className="mt-4 rounded-xl border border-zinc-200">
          <button
            type="button"
            onClick={() => setLegalOpen((o) => !o)}
            aria-expanded={legalOpen}
            aria-controls="legal-panel"
            className="flex w-full items-center justify-between rounded-xl px-4 py-3 text-left text-sm font-medium text-zinc-700 transition-colors duration-150 ease-in-out hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
          >
            Read full Terms &amp; Privacy Policy
            <span aria-hidden className="text-zinc-400">
              {legalOpen ? "−" : "+"}
            </span>
          </button>
          {legalOpen && (
            <div
              id="legal-panel"
              className="max-h-80 space-y-6 overflow-y-auto border-t border-zinc-200 px-4 py-4"
            >
              <section aria-label="Terms of Service">
                <h2 className="mb-2 text-sm font-semibold text-zinc-700">
                  Terms of Service
                </h2>
                <MarkdownDoc src={`${BASE}tos.md`} title="Terms of Service" />
              </section>
              <section aria-label="Privacy Policy">
                <h2 className="mb-2 text-sm font-semibold text-zinc-700">
                  Privacy Policy
                </h2>
                <MarkdownDoc src={`${BASE}privacy.md`} title="Privacy Policy" />
              </section>
            </div>
          )}
        </div>

        <div className="mt-6">
          <Checkbox
            id="agree"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            label="I have read and agree to the Terms of Service and Privacy Policy"
          />
        </div>

        <div className="mt-8 flex justify-end">
          <Button disabled={!agreed} onClick={() => navigate("/info")}>
            Continue →
          </Button>
        </div>
      </Card>
    </ScreenLayout>
  );
}
