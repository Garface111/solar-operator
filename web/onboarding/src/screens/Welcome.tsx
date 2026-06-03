import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Checkbox } from "../ui/Checkbox";

const SERVICES = [
  "Auto-pull GMP bills",
  "NEPOOL-format Excel reports",
  "Email delivery to your clients",
  "Multi-client portal",
];

// Placeholder legal copy — real TOS + Privacy land in Task 11.
const TOS_PLACEHOLDER =
  "Lorem ipsum dolor sit amet, consectetur adipiscing elit. These Terms of " +
  "Service govern your use of Solar Operator. Placeholder text — final terms " +
  "are pending legal review (Task 11). By continuing you agree to be bound by " +
  "the finalized Terms once published.";
const PRIVACY_PLACEHOLDER =
  "Solar Operator accesses your Green Mountain Power portal data solely to " +
  "generate net-metering reports on your behalf. Placeholder text — the final " +
  "Privacy Policy is ported in Task 11. We never sell your data.";

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
            from $45/array/month, billed monthly · $250 one-time setup
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

        <div className="mt-8 rounded-xl border border-zinc-200">
          <button
            type="button"
            onClick={() => setLegalOpen((o) => !o)}
            aria-expanded={legalOpen}
            className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-medium text-zinc-700"
          >
            Terms of Service &amp; Privacy Policy
            <span aria-hidden className="text-zinc-400">
              {legalOpen ? "−" : "+"}
            </span>
          </button>
          {legalOpen && (
            <div className="space-y-4 border-t border-zinc-200 px-4 py-4 text-xs leading-relaxed text-zinc-500">
              <div>
                <h2 className="mb-1 font-semibold text-zinc-700">
                  Terms of Service
                </h2>
                <p>{TOS_PLACEHOLDER}</p>
              </div>
              <div>
                <h2 className="mb-1 font-semibold text-zinc-700">
                  Privacy Policy
                </h2>
                <p>{PRIVACY_PLACEHOLDER}</p>
              </div>
            </div>
          )}
        </div>

        <div className="mt-6">
          <Checkbox
            id="agree"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            label="I agree to the Terms of Service and Privacy Policy"
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
