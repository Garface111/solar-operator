// Done.tsx — the "celebration screen" that previously held confetti +
// "you're in" copy. Replaced with an instant redirect to the dashboard,
// where the new CaptureCeremony component runs the actual sublime moment
// in the context where reality lives (captures cascade in as they land,
// "sign into another portal" CTA, real-time data). The screen still does
// the necessary backend work (completeOnboarding → fresh session token)
// but no longer pretends the work happened when it didn't.
//
// Why instant redirect rather than a 1-2s celebration: when the operator
// got here without a capture (the most common path during testing), the
// old screen lied — it said "your client landed" while the dashboard had
// nothing. The dashboard ceremony tells the truth in either case: either
// the cards are there with cascading chips, or the prompt says "waiting
// for your first capture — log into a portal."

import { useEffect, useRef, useState } from "react";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Spinner } from "../ui/Spinner";
import { getToken, completeOnboarding } from "../lib/onboarding";

// Point directly at /clients so React Router's index redirect doesn't
// strip the ?fresh=1 query param before ClientsSection can read it.
const DASHBOARD_URL = "https://solaroperator.org/accounts/clients?fresh=1";

export default function Done() {
  const [error, setError] = useState<string | null>(null);
  const completedRef = useRef(false);

  useEffect(() => {
    const token = getToken();
    if (!token || completedRef.current) return;
    completedRef.current = true;

    async function finish() {
      try {
        const result = await completeOnboarding(token!);
        if (result.session_token) {
          // Stash the freshly minted dashboard session so the redirect
          // below lands the operator already signed in — no magic-link
          // detour, no separate login screen.
          localStorage.setItem("so_session", result.session_token);
        }
      } catch (e) {
        // If completion fails the operator can still reach the dashboard
        // via the magic-link they got in email; surface a soft error.
        setError(
          e instanceof Error
            ? e.message
            : "Couldn't finalize — try the magic-link in your email.",
        );
        return;
      }
      // Jump to the dashboard. ?fresh=1 turns on the CaptureCeremony
      // pre-emptive surface so the first thing they see is the "your
      // accounts are landing here" panel.
      window.location.replace(DASHBOARD_URL);
    }

    void finish();
  }, []);

  return (
    <ScreenLayout current={4}>
      <div className="flex flex-col items-center gap-4 py-12 text-center">
        {!error ? (
          <>
            <Spinner />
            <p className="text-sm text-zinc-500">
              Finishing up — taking you to your dashboard…
            </p>
          </>
        ) : (
          <>
            <div className="text-3xl">🌿</div>
            <p className="text-base font-semibold text-zinc-900">
              You&apos;re in.
            </p>
            <p className="max-w-sm text-sm text-zinc-500">{error}</p>
            <a
              href={DASHBOARD_URL}
              className="mt-2 inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-primary-600"
            >
              Go to your dashboard →
            </a>
          </>
        )}
      </div>
    </ScreenLayout>
  );
}
