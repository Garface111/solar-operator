import { Link } from "react-router-dom";

/** Shown when the operator has no arrays configured and no report history. */
export function ReportsEmptyState() {
  return (
    <div className="flex flex-col items-center gap-4 rounded-xl border border-cream-border bg-cream px-8 py-12 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-wood-100">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-7 w-7 text-wood-500"
          aria-hidden
        >
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M2 12h2M20 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
        </svg>
      </div>

      <div>
        <p className="text-sm font-medium text-zinc-700">No reports yet</p>
        <p className="mt-1.5 max-w-xs text-sm leading-relaxed text-zinc-500">
          Reports generate once arrays have a full quarter of captured data.
          <br />
          <Link
            to="/clients"
            className="mt-2 inline-block font-medium text-primary-600 underline-offset-2 hover:underline"
          >
            Add arrays in Clients ↗
          </Link>
        </p>
      </div>
    </div>
  );
}
