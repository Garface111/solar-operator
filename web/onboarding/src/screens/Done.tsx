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
//
// Ford Jun 7'26: switched the redirect target from an absolute URL
// (https://solaroperator.org/accounts/?fresh=1) to a same-origin relative
// path (/accounts/?fresh=1). Hardcoding the production hostname meant
// previews, staging, and any non-prod environment redirected off-host;
// Netlify's redirect rules ALSO occasionally misbehaved on the absolute
// URL (intermittent landing-page bounces — once observed, once not).
// Same-origin is safer in every case. Belt: window.location.assign + a
// short verification delay so flaky proxies don't leave the user staring
// at a spinner.

import { useEffect, useRef, useState } from "react";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Spinner } from "../ui/Spinner";
import { getToken, completeOnboarding } from "../lib/onboarding";
import { SO_OPERATOR_PASSWORD_KEY } from "./Info";

// Same-origin relative path so the wizard at /onboarding/* and the dashboard
// at /accounts/* always speak the same hostname. ?fresh=1 flag turns on the
// CaptureCeremony pre-emptive surface; t=... cachebusts BFCache so a hard
// refresh always loads a clean shell.
const DASHBOARD_PATH = "/accounts/?fresh=1";

export default function Done() {
  const [error, setError] = useState<string | null>(null);
  const completedRef = useRef(false);

  useEffect(() => {
    const token = getToken();
    if (!token || completedRef.current) return;
    completedRef.current = true;

    async function finish() {
      try {
        // Pull the password the operator chose on /info (if any) so we
        // can hash+store it server-side in the same /complete call that
        // mints their session. Cleared from sessionStorage the moment
        // the POST succeeds — never lingers.
        const stashedPassword =
          sessionStorage.getItem(SO_OPERATOR_PASSWORD_KEY) || undefined;
        const result = await completeOnboarding(
          token!,
          stashedPassword ? { password: stashedPassword } : undefined,
        );
        // Wipe the stashed plaintext password — server has the hash now.
        try {
          sessionStorage.removeItem(SO_OPERATOR_PASSWORD_KEY);
        } catch { /* sessionStorage can be locked down */ }
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
      // Jump to the dashboard. Same-origin relative + cachebust query.
      // ?fresh=1 turns on the CaptureCeremony pre-emptive surface so the
      // first thing they see is the "your accounts are landing here" panel.
      const url = `${DASHBOARD_PATH}&t=${Date.now()}`;
      window.location.assign(url);
      // Belt: if assign() is short-circuited by a flaky proxy / BFCache
      // edge case (the intermittent "landed on homepage" bug Ford caught
      // Jun 7'26), force a hard navigation 800ms later. By then any real
      // navigation has already torn down this effect.
      window.setTimeout(() => {
        try {
          if (typeof window !== "undefined") {
            window.location.href = url;
          }
        } catch { /* navigation already in flight */ }
      }, 800);
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
              href={DASHBOARD_PATH}
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
