import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { createCheckout, setToken, type ClientSeedPayload } from "../lib/onboarding";
import { SO_OPERATOR_KEY, type OperatorInfo } from "./Info";
import {
  SO_CLIENTS_DRAFT_KEY,
  SO_ARRAY_ESTIMATE_KEY,
  type ClientDraftEntry,
} from "./ClientSetup";

const ARRAY_PRICE = 45;
const SETUP_FEE = 250;

function readOperatorInfo(locationState: unknown): OperatorInfo | null {
  if (locationState && typeof (locationState as OperatorInfo).email === "string") {
    return locationState as OperatorInfo;
  }
  try {
    const raw = sessionStorage.getItem(SO_OPERATOR_KEY);
    return raw ? (JSON.parse(raw) as OperatorInfo) : null;
  } catch {
    return null;
  }
}

function readClientsDraft(): ClientDraftEntry[] {
  try {
    const raw = sessionStorage.getItem(SO_CLIENTS_DRAFT_KEY);
    return raw ? (JSON.parse(raw) as ClientDraftEntry[]) : [];
  } catch {
    return [];
  }
}

export default function Plan() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const info = readOperatorInfo(location.state);
  const clientsDraft = readClientsDraft();

  const totalArrays = clientsDraft.reduce((n, c) => n + (c.arrays?.length ?? 0), 0);

  // Read the operator's array-count estimate from the previous screen.
  // Falls back to total entered arrays (legacy path) or 1.
  const initialEstimate = (() => {
    try {
      const raw = sessionStorage.getItem(SO_ARRAY_ESTIMATE_KEY);
      const n = raw ? parseInt(raw, 10) : NaN;
      if (Number.isFinite(n) && n > 0) return n;
    } catch {
      /* noop */
    }
    return Math.max(1, totalArrays || 1);
  })();

  const [estimate, setEstimate] = useState(initialEstimate);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!info?.email) {
      navigate("/info", { replace: true });
    }
  }, []);

  if (!info?.email) return null;

  const quantity = totalArrays > 0 ? totalArrays : estimate;
  const monthly = quantity * ARRAY_PRICE;
  const todayTotal = SETUP_FEE + monthly;
  const nextDate = new Date(new Date().getFullYear(), new Date().getMonth() + 1, 1);
  const nextMonth = nextDate.toLocaleDateString("en-US", { month: "long", year: "numeric" });

  async function goToStripe() {
    if (!info) return;
    setSubmitting(true);
    try {
      const payload: Parameters<typeof createCheckout>[0] = {
        full_name: info.fullName,
        email: info.email,
        company: info.company,
      };
      if (clientsDraft.length > 0) {
        const seeds: ClientSeedPayload[] = clientsDraft.map((c) => ({
          name: c.name,
          contact_email: c.contact_email,
          arrays: (c.arrays ?? []).map((a) => ({
            name: a.name,
            nepool_gis_id: a.nepool_gis_id,
          })),
        }));
        payload.clients = seeds;
      } else {
        payload.array_count = estimate;
      }

      const { checkout_url, onboarding_token } = await createCheckout(payload);
      setToken(onboarding_token);
      window.location.href = checkout_url;
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't reach checkout. Check your connection and try again.",
      );
      setSubmitting(false);
    }
  }

  return (
    <ScreenLayout current={3}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Your bill — no surprises.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          Review exactly what you&apos;ll pay before going to checkout.
        </p>

        {/* Billing breakdown */}
        <div className="mt-6 rounded-xl border border-zinc-200 bg-zinc-50 p-5 space-y-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-600">One-time setup</span>
            <span className="font-medium text-zinc-900">${SETUP_FEE}</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-zinc-600">
              Monthly ({quantity} {quantity === 1 ? "array" : "arrays"} × ${ARRAY_PRICE})
            </span>
            <span className="font-medium text-zinc-900">${monthly}/month</span>
          </div>
          <div className="border-t border-zinc-200 pt-3 flex items-center justify-between text-sm font-medium">
            <span className="text-zinc-700">Charged today</span>
            <span className="text-zinc-900">${todayTotal}</span>
          </div>
          <p className="text-xs text-zinc-500">
            Then ${monthly}/month starting {nextMonth}.
          </p>
        </div>

        {/* Client summary (if pre-entered) */}
        {clientsDraft.length > 0 && (
          <ul className="mt-5 space-y-2">
            {clientsDraft.map((c, i) => {
              const arrCount = c.arrays?.length ?? 0;
              return (
                <li key={i} className="rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-zinc-900">{c.name}</span>
                    <span className="shrink-0 rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-medium text-primary-700">
                      {arrCount} {arrCount === 1 ? "array" : "arrays"}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* Estimate adjuster (shown only when no pre-entered arrays) */}
        {totalArrays === 0 && (
          <div className="mt-6 rounded-xl border border-zinc-200 p-4">
            <label
              htmlFor="estimate-count"
              className="block text-sm font-medium text-zinc-700"
            >
              Adjust array count estimate
            </label>
            <input
              id="estimate-count"
              type="number"
              min={1}
              value={estimate}
              onChange={(e) =>
                setEstimate(Math.max(1, parseInt(e.target.value, 10) || 1))
              }
              className="mt-1.5 block w-28 rounded-xl border border-zinc-300 px-3 py-2 text-sm text-zinc-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
            />
            <p className="mt-1 text-xs text-zinc-500">
              True-up happens automatically as GMP captures come in — your card won&apos;t
              be charged extra without notice.
            </p>
          </div>
        )}

        <div className="mt-8 flex items-center justify-between">
          <Button
            variant="ghost"
            onClick={() => navigate("/client-setup")}
            disabled={submitting}
          >
            ← Back
          </Button>
          <Button onClick={goToStripe} disabled={submitting}>
            {submitting ? (
              <>
                <Spinner />
                Redirecting…
              </>
            ) : (
              "Continue to payment →"
            )}
          </Button>
        </div>

        <p className="mt-4 text-center text-xs text-zinc-400">
          Secure checkout powered by Stripe. We never store your card details.
        </p>
      </Card>
    </ScreenLayout>
  );
}
