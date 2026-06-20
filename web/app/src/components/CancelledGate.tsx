interface Props {
  /** Sign the operator out (shown as a secondary, low-emphasis action). */
  onSignOut: () => void;
  /** Product-aware support address shown for reactivation. */
  supportEmail?: string;
}

/**
 * Full-page block shown when an account has been cancelled
 * (subscription_status === "cancelled" | "canceled" and active === false).
 *
 * A cancelled account must NOT load the working dashboard — otherwise
 * cancelling appears to do nothing (the operator can still browse and act).
 * This replaces the dashboard content with an unambiguous "cancelled" state.
 *
 * Friendly + data-safe: nothing is deleted. Reactivation is a deliberate,
 * human step (reply / email support) rather than an auto-resume, because the
 * add-card auto-resume path is wired only for the `paused_no_card` state.
 */
export function CancelledGate({ onSignOut, supportEmail = "admin@solaroperator.org" }: Props) {
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
          Your account is cancelled
        </h1>
        <p className="mb-1 text-sm leading-relaxed text-zinc-600">
          This account has been cancelled, so the dashboard and automatic bill
          pulls are turned off. You won&apos;t be charged.
        </p>
        <p className="mb-6 text-sm font-medium leading-relaxed text-emerald-700">
          Your data is safe — we haven&apos;t deleted anything. Want to come
          back? Just let us know and we&apos;ll reactivate it.
        </p>

        <a
          href={`mailto:${supportEmail}?subject=Reactivate my account`}
          className="block w-full rounded-xl bg-emerald-600 px-4 py-3 text-sm font-semibold text-white transition-colors hover:bg-emerald-700"
        >
          Email us to reactivate →
        </a>

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
