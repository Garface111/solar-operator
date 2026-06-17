import { useState, useCallback } from "react";
import { addPaymentMethod } from "../lib/api";
import { useToast } from "../ui/Toast";

interface Props {
  /** Sign the operator out (shown as a secondary, low-emphasis action). */
  onSignOut: () => void;
}

/**
 * Full-page block shown when a 14-day trial ends with no card on file
 * (subscription_status === "paused_no_card"). Replaces the dashboard content:
 * the operator must add a card to regain access. Reassures them their data is
 * safe and waiting — nothing is deleted if they come back later.
 *
 * This is intentionally a HARD gate (not just the banner) so "add a card or
 * lose access" is unambiguous, while staying friendly: read-only data is held,
 * not destroyed.
 */
export function TrialEndedGate({ onSignOut }: Props) {
  const toast = useToast();
  const [adding, setAdding] = useState(false);

  const startAddCard = useCallback(async () => {
    setAdding(true);
    try {
      await addPaymentMethod(); // redirects to Stripe Checkout (setup mode)
    } catch (err) {
      setAdding(false);
      toast.error(
        err instanceof Error ? err.message : "Couldn't open the add-card page",
      );
    }
  }, [toast]);

  return (
    <div className="flex min-h-[70vh] items-center justify-center px-4 py-12">
      <div className="w-full max-w-md rounded-2xl border border-amber-200 bg-white p-8 text-center shadow-sm">
        {/* Lock-ish glyph */}
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-amber-100">
          <svg
            className="h-7 w-7 text-amber-600"
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
          Your free trial has ended
        </h1>
        <p className="mb-1 text-sm leading-relaxed text-zinc-600">
          Add a payment method to keep using your dashboard and resume your
          reports.
        </p>
        <p className="mb-6 text-sm font-medium leading-relaxed text-emerald-700">
          Your data is safe — we haven&apos;t deleted anything. Come back
          anytime and everything will be right where you left it.
        </p>

        <button
          type="button"
          onClick={startAddCard}
          disabled={adding}
          className="w-full rounded-xl bg-amber-600 px-4 py-3 text-sm font-semibold text-white transition-colors hover:bg-amber-700 disabled:opacity-60"
        >
          {adding ? "Opening secure checkout…" : "Add a card to resume →"}
        </button>

        <p className="mt-4 text-xs text-zinc-400">
          $15 per solar array / month · cancel anytime · no charge until you add
          a card.
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
