import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";

/**
 * Connect fork — how utility bills get pulled:
 *   • Store it with us (Cloud Capture) → /cloud
 *   • Keep it on my computer (extension) → /extension
 *
 * Mirrors Array Operator's dual-path choice. Cloud is recommended; extension
 * stays first-class for operators who want passwords only on-device.
 */
export default function Connect() {
  const navigate = useNavigate();

  return (
    <ScreenLayout current={3}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Your data, your choice.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          Two ways to keep client utility bills fresh. Pick whichever fits — you can
          switch later from Master account.
        </p>

        <div className="mt-6 grid gap-4 sm:grid-cols-2">
          <button
            type="button"
            onClick={() => navigate("/cloud")}
            className="group flex flex-col rounded-2xl border-2 border-primary-500 bg-primary-50 p-5 text-left transition-shadow hover:shadow-md focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
          >
            <span className="inline-flex w-fit items-center rounded-full bg-primary-500 px-2.5 py-0.5 text-[11px] font-semibold text-white">
              Recommended · zero effort
            </span>
            <h2 className="mt-3 text-base font-semibold text-zinc-900">Store it with us</h2>
            <p className="mt-1 flex-1 text-sm text-zinc-600">
              Enter a utility login once and our servers sign in and pull the bills
              around the clock — no install, no tab to keep open. Your password is
              encrypted at rest; remove it anytime.
            </p>
            <span className="mt-4 inline-flex text-sm font-semibold text-primary-700 group-hover:underline">
              Set up cloud refresh →
            </span>
          </button>

          <button
            type="button"
            onClick={() => navigate("/extension")}
            className="group flex flex-col rounded-2xl border border-zinc-200 bg-white p-5 text-left transition-shadow hover:shadow-md focus:outline-none focus-visible:ring-2 focus-visible:ring-zinc-300"
          >
            <span className="inline-flex w-fit items-center rounded-full bg-zinc-100 px-2.5 py-0.5 text-[11px] font-semibold text-zinc-600">
              Most private
            </span>
            <h2 className="mt-3 text-base font-semibold text-zinc-900">
              Keep it on my computer
            </h2>
            <p className="mt-1 flex-1 text-sm text-zinc-600">
              Install the free browser extension. It captures bills while you&apos;re
              signed in to your utility — your passwords never leave your device.
            </p>
            <span className="mt-4 inline-flex text-sm font-semibold text-zinc-700 group-hover:underline">
              Use the extension →
            </span>
          </button>
        </div>

        <p className="mt-5 text-xs text-zinc-400">
          Either way, nothing is billed to your clients until you review and approve it.
        </p>
      </Card>
    </ScreenLayout>
  );
}
