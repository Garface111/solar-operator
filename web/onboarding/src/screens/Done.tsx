import { useEffect, useState } from "react";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { getToken, fetchStatus, type OnboardingStatus } from "../lib/onboarding";

const DASHBOARD_URL = "https://solaroperator.org/app";

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
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-primary-100 text-3xl text-primary-600">
          ✓
        </div>
        <h1 className="mt-6 text-2xl font-semibold tracking-tight text-zinc-900">
          You&apos;re all set.
        </h1>
        <p className="mx-auto mt-2 max-w-md text-sm text-zinc-500">
          Check your inbox for your login link. We&apos;ve emailed you a secure
          link to your dashboard — no password needed.
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
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-primary-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2"
          >
            Open your inbox →
          </a>
        </div>
      </Card>
    </ScreenLayout>
  );
}
