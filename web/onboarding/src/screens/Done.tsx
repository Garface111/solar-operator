import { useEffect, useRef, useState } from "react";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { getToken, fetchStatus, type OnboardingStatus } from "../lib/onboarding";

// Absolute marketing-domain URL. The dashboard uses the same magic-link auth as
// the email link, so this CTA just lands the operator on the sign-in screen.
const DASHBOARD_URL = "https://solaroperator.org/accounts/";
const GMP_URL = "https://mypower.greenmountainpower.com/";

export default function Done() {
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [extensionActive, setExtensionActive] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    const token = getToken();
    if (!token) return;
    let cancelled = false;

    // Initial fetch
    fetchStatus(token)
      .then((s) => {
        if (!cancelled) {
          setStatus(s);
          if (s.extension_active) setExtensionActive(true);
        }
      })
      .catch(() => {});

    // Poll every 4 seconds until extension_active flips true
    pollRef.current = setInterval(() => {
      fetchStatus(token)
        .then((s) => {
          if (!cancelled) {
            setStatus(s);
            if ((s as any).extension_active) {
              setExtensionActive(true);
              if (pollRef.current) clearInterval(pollRef.current);
            }
          }
        })
        .catch(() => {});
    }, 4000);

    return () => {
      cancelled = true;
      if (pollRef.current) clearInterval(pollRef.current);
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
          You&apos;re set up. One last step.
        </h1>
        <p className="mx-auto mt-2 max-w-md text-sm text-zinc-500">
          Log into Green Mountain Power so the extension can start capturing
          your bill data.
        </p>

        {/* GMP CTA — flips to a success state once the extension heartbeats */}
        <div className="mt-8">
          {extensionActive ? (
            <div className="mx-auto inline-flex max-w-sm items-center gap-2.5 rounded-xl border border-primary-200 bg-primary-50 px-5 py-3 text-sm font-medium text-primary-700">
              <span aria-hidden className="text-lg">✓</span>
              Extension active on GMP — capturing your data now
            </div>
          ) : (
            <>
              <a
                href={GMP_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-6 py-3 text-sm font-semibold text-white transition-colors duration-150 ease-in-out hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
              >
                Open greenmountainpower.com →
              </a>
              <p className="mt-2 text-xs text-zinc-400">
                This page will update automatically once the extension sees your GMP session.
              </p>
            </>
          )}
        </div>

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

        {status && status.arrays_count > 0 && (
          <p className="mx-auto mt-6 max-w-md text-sm text-zinc-600">
            Your subscription is now billed for{" "}
            <span className="font-medium text-zinc-900">
              {status.arrays_count}{" "}
              {status.arrays_count === 1 ? "array" : "arrays"}
            </span>{" "}
            at $45 each ={" "}
            <span className="font-medium text-zinc-900">
              ${status.arrays_count * 45}/month
            </span>
            .
          </p>
        )}

        {status && status.arrays_count === 0 && (
          <p className="mx-auto mt-4 max-w-md text-xs text-zinc-400">
            Arrays from auto-populate clients will appear once they sign into GMP
            through the extension. Your monthly total updates automatically as
            they do.
          </p>
        )}

        <div className="mt-8">
          <a
            href={DASHBOARD_URL}
            className="text-sm text-zinc-500 underline underline-offset-2 hover:text-zinc-700"
          >
            Or go to your account dashboard →
          </a>
        </div>
      </Card>
    </ScreenLayout>
  );
}
