import { useEffect, useRef, useState } from "react";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { getToken, completeOnboarding, fetchStatus, type OnboardingStatus } from "../lib/onboarding";

const DASHBOARD_URL = "https://solaroperator.org/accounts/";

// Confetti pieces: [dx, dy, color, size, delay]
const PIECES: [number, number, string, number, number][] = [
  [-60, -80,  "#047857", 10, 0],
  [ 60, -80,  "#d1fae5", 8,  60],
  [-40, -100, "#34d399", 12, 120],
  [ 40, -100, "#6ee7b7", 9,  30],
  [-80, -60,  "#047857", 7,  90],
  [ 80, -60,  "#d1fae5", 11, 150],
  [  0, -110, "#a7f3d0", 10, 45],
  [-70, -90,  "#34d399", 8,  180],
  [ 70, -90,  "#047857", 9,  20],
];

export default function Done() {
  const [status, setStatus] = useState<OnboardingStatus | null>(null);
  const [completing, setCompleting] = useState(true);
  const [showConfetti, setShowConfetti] = useState(false);
  const completedRef = useRef(false);

  useEffect(() => {
    const token = getToken();
    if (!token || completedRef.current) return;
    completedRef.current = true;

    async function finish() {
      try {
        const result = await completeOnboarding(token!);
        if (result.session_token) {
          localStorage.setItem("so_session", result.session_token);
        }
      } catch {
        // Non-fatal — the operator can still reach the dashboard via magic-link.
      }

      try {
        const s = await fetchStatus(token!);
        setStatus(s);
      } catch {
        // Non-fatal — stats are decorative on this screen.
      }

      setCompleting(false);
      // Small delay so the card renders before confetti fires.
      window.setTimeout(() => setShowConfetti(true), 100);
    }

    void finish();
  }, []);

  return (
    <ScreenLayout current={5}>
      <Card active className="text-center">
        {/* Certificate unlock badge */}
        <div className="relative mx-auto inline-block">
          <div
            aria-hidden
            className="mx-auto flex h-20 w-20 items-center justify-center rounded-full border-4 border-primary-200 bg-primary-50 text-4xl"
          >
            🌿
          </div>

          {/* Confetti burst */}
          {showConfetti && (
            <div aria-hidden className="pointer-events-none absolute inset-0 flex items-center justify-center">
              {PIECES.map(([dx, dy, color, size, delay], i) => (
                <span
                  key={i}
                  className="confetti-piece absolute rounded-full"
                  style={{
                    width: size,
                    height: size,
                    backgroundColor: color,
                    "--cx": "0px",
                    "--cy": "0px",
                    "--dx": `${dx}px`,
                    "--dy": `${dy}px`,
                    animationDelay: `${delay}ms`,
                  } as React.CSSProperties}
                />
              ))}
            </div>
          )}
        </div>

        {/* Welcome certificate */}
        <div className="mt-6">
          <div className="mx-auto max-w-sm rounded-2xl border-2 border-primary-200 bg-gradient-to-b from-primary-50 to-white px-6 py-5">
            <p className="text-xs font-semibold uppercase tracking-widest text-primary-600">
              Welcome to Solar Operator
            </p>
            <h1 className="mt-2 text-2xl font-semibold tracking-tight text-zinc-900">
              You&apos;re in.
            </h1>
            <p className="mt-2 text-sm text-zinc-500">
              Quarterly NEPOOL reports, fully automated. You&apos;ve just bought
              back hours every quarter.
            </p>
            {!completing && status && status.arrays_count > 0 && (
              <p className="mt-3 text-sm font-medium text-primary-700">
                {status.arrays_count} {status.arrays_count === 1 ? "array" : "arrays"} ·{" "}
                ${status.arrays_count * 45}/month
              </p>
            )}
          </div>
        </div>

        {/* What happens next */}
        <div className="mx-auto mt-6 max-w-sm rounded-xl border border-zinc-200 bg-zinc-50 px-5 py-4 text-left">
          <p className="text-xs font-semibold text-zinc-700">What happens next</p>
          <ol className="mt-3 space-y-2">
            {[
              "Log into your utility portal (GMP, VEC, and others) from any tab — the extension captures bills in the background.",
              "We generate your first NEPOOL report at the end of the current quarter.",
              "Your clients get their report by email. You get a copy too.",
            ].map((step, i) => (
              <li key={i} className="flex items-start gap-2.5 text-xs text-zinc-600">
                <span
                  aria-hidden
                  className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-primary-100 text-[10px] font-semibold text-primary-700"
                >
                  {i + 1}
                </span>
                {step}
              </li>
            ))}
          </ol>
        </div>

        {/* Stats */}
        {!completing && status && (
          <div className="mx-auto mt-6 grid max-w-xs grid-cols-2 gap-3">
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
                {status.arrays_count === 1 ? "array" : "arrays"}
              </div>
            </div>
          </div>
        )}

        {/* Dashboard CTA */}
        <div className="mt-8">
          <a
            href={DASHBOARD_URL}
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-primary-500 px-6 py-3 text-sm font-semibold text-white transition-colors duration-150 ease-in-out hover:bg-primary-600 active:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
          >
            Go to your dashboard →
          </a>
        </div>

        <div className="mt-4">
          <a
            href={DASHBOARD_URL}
            className="text-sm text-zinc-500 underline underline-offset-2 hover:text-zinc-700"
          >
            Sign in with magic link →
          </a>
        </div>
      </Card>
    </ScreenLayout>
  );
}
