import { useState, useCallback } from "react";
import { reactivateAccount } from "../lib/api";
import { useToast } from "../ui/Toast";

interface Props {
  /** Sign the operator out (shown as a secondary, low-emphasis action). */
  onSignOut: () => void;
}

/**
 * Full-page block shown when an account has been cancelled
 * (subscription_status === "cancelled" | "canceled" and active === false).
 *
 * A cancelled account must NOT load the working dashboard — otherwise
 * cancelling appears to do nothing (the operator can still browse and act).
 * This replaces the dashboard content with an unambiguous "cancelled" state and
 * a single clear action: start your subscription again.
 *
 * Reactivation starts a fresh PAID subscription with NO trial — the operator
 * already used their trial. reactivateAccount() sends them to Stripe Checkout to
 * add a card; the setup_intent.succeeded webhook then creates the subscription
 * (no trial) and flips them back to active. Data is preserved throughout.
 */
export function CancelledGate({ onSignOut }: Props) {
  const toast = useToast();
  const [starting, setStarting] = useState(false);

  const startReactivate = useCallback(async () => {
    setStarting(true);
    try {
      await reactivateAccount(); // redirects to Stripe Checkout (setup mode)
    } catch (err) {
      setStarting(false);
      toast.error(
        err instanceof Error ? err.message : "Couldn't open secure checkout",
      );
    }
  }, [toast]);

  return (
    <div className="flex min-h-[70vh] items-center justify-center px-4 py-12">
      <div className="w-full max-w-md rounded-2xl border border-zinc-200 bg-white p-8 text-center shadow-sm">
        {/* Lock glyph */}
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-zinc-100">
          <svg
            className="h-7 w-7 text-zinc-500"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <rect x="3" y="11" width="18" height="11" rx="2" />
            <path d="M7 11V7a5 5 0 0 1 10 0v4" />
          </svg>
        </div>

        <h1 className="mb-2 text-xl font-semibold text-zinc-900">
          Your subscription is cancelled
        </h1>
        <p className="mb-1 text-sm leading-relaxed text-zinc-600">
          Your dashboard and automatic bill pulls are turned off. Start your
          subscription again to pick up right where you left off.
        </p>
        <p className="mb-6 text-sm font-medium leading-relaxed text-emerald-700">
          Your data is safe — we haven&apos;t deleted anything.
        </p>

        <button
          type="button"
          onClick={startReactivate}
          disabled={starting}
          className="w-full rounded-xl bg-emerald-600 px-4 py-3 text-sm font-semibold text-white transition-colors hover:bg-emerald-700 disabled:opacity-60"
        >
          {starting ? "Opening secure checkout…" : "Start my subscription →"}
        </button>

        <p className="mt-4 text-xs text-zinc-400">
          Billing starts today — your free trial has already been used. Cancel
          anytime.
        </p>

        <button
          type="button"
          onClick={onSignOut}
          className="mt-6 text-xs text-zinc-400 underline-offset-2 hover:text-zinc-600 hover:underline"
        >
          Sign out
        </button>
      </div>
    </div>
  );
}
