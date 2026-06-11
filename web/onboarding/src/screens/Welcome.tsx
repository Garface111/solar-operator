import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Checkbox";
import { MarkdownDoc } from "../ui/MarkdownDoc";

const SERVICES = [
  "Auto-pull bills from your utility — hundreds supported nationwide",
  "NEPOOL-format Excel reports",
  "Email delivery to your clients",
  "Multi-client portal",
];

// Plain-English bullets mirrored from web/onboarding/public/privacy.md
// and tos.md (Quick summary sections). If you edit those files, update here.
const PP_BULLETS = [
  "We never sell your data — it's only used to run the reporting service.",
  "We only read your utility billing data — nothing else from your browser.",
  "You can delete everything by emailing admin@solaroperator.org. We purge your data within 24 hours.",
  "Your utility login session expires automatically (~21 days) and is refreshed each time you log in with the extension active.",
  "Our only email provider is Resend.com — it delivers your reports and sign-in links. No other third party sees your data.",
];

const TOS_BULLETS = [
  "Free for 14 days — no card required. Add a payment method from your dashboard before the trial ends to keep reports flowing.",
  "After trial: $250 one-time setup, then $15 per solar array per month, billed monthly.",
  "Cancel anytime — no penalty. Your data stays accessible for 30 days after cancellation.",
  "You own your data. We use it only to run the service. We never sell it.",
  "You're responsible for entering correct NEPOOL-GIS IDs — we don't verify them.",
  "Supports hundreds of utilities nationwide — co-ops, municipals, and Green Mountain Power are automated today, with more added every week. Don't see yours? Request it.",
];

// Served from web/onboarding/public/ by Vite (and FastAPI in prod) under the
// app's base path, e.g. /onboarding/tos.md.
const BASE = import.meta.env.BASE_URL;

export default function Welcome() {
  const navigate = useNavigate();
  const [agreed, setAgreed] = useState(false);
  // Outer panel (the plain-English bullets) opens by default so the user
  // sees the gist without an extra click. The nested "full legal text"
  // accordion stays collapsed so the page doesn't feel like a wall.
  // Ford Jun 8'26: first bulleted dropdown should be auto-revealed.
  const [termsOpen, setTermsOpen] = useState(true);
  const [legalOpen, setLegalOpen] = useState(false);

  return (
    <ScreenLayout current={0}>
      <Card active>
        <h1 className="text-3xl font-semibold tracking-tight text-zinc-900">
          Quarterly solar reports, on autopilot.
        </h1>
        <p className="mt-3 text-base text-zinc-500">
          We pull your utility bills — hundreds of utilities supported coast
          to coast — build NEPOOL-format net-metering credit reports, and
          email them to your clients every quarter, so you never touch a
          spreadsheet again.
        </p>

        <div className="mt-8 rounded-xl border border-primary-200 bg-primary-50 p-5">
          <p className="text-sm font-semibold text-primary-800">
            14-day free trial · $250 setup + $15/array/month after (volume discounts past 50 arrays) · cancel anytime
          </p>
          <p className="mt-1 text-xs text-primary-700">
            Trial starts the moment you finish signup — no card needed today.
            Add your card from the Accounts tab whenever you&apos;re ready.
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

        {/* Checkbox sits above the collapsed Terms block so users can agree
            without being forced to scroll through the full text first. */}
        <div className="mt-8">
          <Checkbox
            id="agree"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            label="I agree to the Terms of Service and Privacy Policy"
          />
        </div>

        {/* Terms & Privacy — collapsed by default. */}
        <div className="mt-4 rounded-xl border border-zinc-200">
          <button
            type="button"
            onClick={() => setTermsOpen((o) => !o)}
            aria-expanded={termsOpen}
            aria-controls="terms-panel"
            className="flex w-full items-center justify-between rounded-xl px-4 py-3 text-left text-sm font-medium text-zinc-700 transition-colors duration-150 ease-in-out hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
          >
            {termsOpen ? "Hide Terms" : "Read full Terms & Privacy"}
            <span aria-hidden className="text-zinc-400">
              {termsOpen ? "−" : "+"}
            </span>
          </button>

          {termsOpen && (
            <div id="terms-panel">
              {/* Plain-English summary bullets */}
              <div className="rounded-b-none rounded-t-none border-t border-zinc-200 bg-zinc-50 px-5 py-5">
                <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  Privacy &amp; Terms — the short version
                </p>
                <div className="mt-3 grid gap-5 sm:grid-cols-2">
                  <div>
                    <p className="mb-2 text-xs font-semibold text-zinc-700">Privacy</p>
                    <ul className="space-y-1.5">
                      {PP_BULLETS.map((b) => (
                        <li key={b} className="flex items-start gap-2 text-xs text-zinc-600">
                          <span aria-hidden className="mt-0.5 shrink-0 text-primary-500">✓</span>
                          {b}
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div>
                    <p className="mb-2 text-xs font-semibold text-zinc-700">Terms</p>
                    <ul className="space-y-1.5">
                      {TOS_BULLETS.map((b) => (
                        <li key={b} className="flex items-start gap-2 text-xs text-zinc-600">
                          <span aria-hidden className="mt-0.5 shrink-0 text-primary-500">✓</span>
                          {b}
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              </div>

              {/* Full legal text — nested accordion */}
              <div className="border-t border-zinc-200 px-4 py-3">
                <button
                  type="button"
                  onClick={() => setLegalOpen((o) => !o)}
                  aria-expanded={legalOpen}
                  aria-controls="legal-panel"
                  className="flex w-full items-center justify-between rounded-xl px-0 py-1 text-left text-sm font-medium text-zinc-700 transition-colors duration-150 ease-in-out hover:text-zinc-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                >
                  Read full Terms &amp; Privacy Policy
                  <span aria-hidden className="text-zinc-400">
                    {legalOpen ? "−" : "+"}
                  </span>
                </button>
                {legalOpen && (
                  <div
                    id="legal-panel"
                    className="mt-3 max-h-80 space-y-6 overflow-y-auto border-t border-zinc-200 py-4"
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
            </div>
          )}
        </div>

        <div className="mt-8 flex items-center justify-between">
          <Button variant="ghost" onClick={() => navigate("/demo")}>
            ← Back
          </Button>
          <Button disabled={!agreed} onClick={() => navigate("/info")}>
            Continue →
          </Button>
        </div>
      </Card>
    </ScreenLayout>
  );
}
