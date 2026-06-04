import { useEffect, useState } from "react";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { getToken, fetchStatus, type OnboardingStatus } from "../lib/onboarding";

// Absolute marketing-domain URL. The dashboard uses the same magic-link auth as
// the email link, so this CTA just lands the operator on the sign-in screen.
const DASHBOARD_URL = "https://solaroperator.org/accounts/";

export default function Done() {
  const [status, setStatus] = useState<OnboardingStatus | null>(null);

  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let cancelled = false;
    fetchStatus(token)
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {
        /* non-fatal — the success message stands on its own */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <ScreenLayout current={4}>
      <Card active className="text-center">
        <div
          aria-hidden
          className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-primary-100 text-3xl text-primary-600"
        >
          ✓
        </div>
        <h1 className="mt-6 text-2xl font-semibold tracking-tight text-zinc-900">
          You&apos;re all set.
        </h1>
        <p className="mx-auto mt-2 max-w-md text-sm text-zinc-500">
          You&apos;re signed in and ready to go — head straight to your
          dashboard below. We&apos;ve also emailed you a secure sign-in link for
          next time you log in from another browser or device.
        </p>

        {status && (
          <div className="mx-auto mt-8 grid max-w-xs grid-cols-2 gap-3">
            <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3">
              <div className="text-2xl font-semibold text-zinc-900">
                {status.clients_count}
              </div>
              <div className="text-xs text-zinc-500">
                {status.clients_count === 1 ? "client" : "clients"}
              </div>
            </div>
            <div className="rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3">
              <div className="text-2xl font-semibold text-zinc-900">
                {status.arrays_count}
              </div>
              <div className="text-xs text-zinc-500">
                {status.arrays_count === 1 ? "array so far" : "arrays so far"}
              </div>
            </div>
          </div>
        )}

        {status && status.arrays_count === 0 && (
          <p className="mx-auto mt-4 max-w-md text-xs text-zinc-400">
            Arrays from auto-populate clients will appear once they sign into GMP
            through the extension.
          </p>
        )}

        <div className="mt-8 flex justify-center">
          <a
            href={DASHBOARD_URL}
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-5 py-2.5 text-sm font-medium text-white transition-colors duration-150 ease-in-out hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
          >
            Go to your account dashboard →
          </a>
        </div>
      </Card>
    </ScreenLayout>
  );
}
