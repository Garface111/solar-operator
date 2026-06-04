import { useCallback, useEffect, useRef, useState } from "react";
import { markWalkthroughSeen } from "../lib/walkthrough";

interface SpotRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const PAD = 10;
const DIM = "rgba(0,0,0,0.55)";

interface StepDef {
  anchor: string | null;
  title: string;
  body: string;
  cta?: { label: string; href: string };
}

const STEPS: StepDef[] = [
  {
    anchor: null,
    title: "Quick 60-second tour",
    body: "We'll show you the fastest path to working reports.",
  },
  {
    anchor: "2",
    title: "Click a client to expand",
    body: "Click any client to expand it and see their arrays.",
  },
  {
    anchor: "3",
    title: "Enter the utility login",
    body: "Paste the email or username this client uses to log into their utility portal.",
  },
  {
    anchor: "4",
    title: "Toggle auto-populate ON",
    body: "Turn this on so we automatically pull this client's arrays the next time you log into their utility portal — no manual array entry.",
  },
  {
    anchor: null,
    title: "Go log in",
    body: "Now open Green Mountain Power and sign in with that account. We'll capture the arrays in the background and they'll appear here automatically.",
    cta: {
      label: "Open GMP →",
      href: "https://www.greenmountainpower.com/account/",
    },
  },
  {
    anchor: null,
    title: "You're all set",
    body: "When you come back, your client will have all their arrays auto-populated. Pricing reconciles automatically.",
  },
];

interface Props {
  onClose: () => void;
}

export function WalkthroughOverlay({ onClose }: Props) {
  const [step, setStep] = useState(0);
  const [spot, setSpot] = useState<SpotRect | null>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  const current = STEPS[step];
  const isLast = step === STEPS.length - 1;

  function dismiss() {
    markWalkthroughSeen();
    onClose();
  }

  function next() {
    if (isLast) {
      dismiss();
    } else {
      setStep((s) => s + 1);
    }
  }

  const updateSpot = useCallback(() => {
    if (!current.anchor) {
      setSpot(null);
      return;
    }
    const el = document.querySelector<HTMLElement>(
      `[data-tour-step="${current.anchor}"]`,
    );
    if (!el) {
      setSpot(null);
      return;
    }
    const r = el.getBoundingClientRect();
    setSpot({
      top: r.top - PAD,
      left: r.left - PAD,
      width: r.width + PAD * 2,
      height: r.height + PAD * 2,
    });
  }, [current.anchor]);

  useEffect(() => {
    updateSpot();
    window.addEventListener("scroll", updateSpot, { passive: true });
    window.addEventListener("resize", updateSpot);
    return () => {
      window.removeEventListener("scroll", updateSpot);
      window.removeEventListener("resize", updateSpot);
    };
  }, [updateSpot]);

  // Tooltip position: below spotlight if space allows, else above, else centered.
  const tooltipStyle = (() => {
    const base: React.CSSProperties = {
      position: "fixed",
      zIndex: 9999,
      width: 340,
    };
    if (!spot) {
      return { ...base, top: "50%", left: "50%", transform: "translate(-50%,-50%)" };
    }
    const vp = { w: window.innerWidth, h: window.innerHeight };
    const left = Math.max(16, Math.min(spot.left, vp.w - 356));
    const belowTop = spot.top + spot.height + 12;
    const aboveBottom = vp.h - spot.top + 12;
    if (belowTop + 200 <= vp.h) {
      return { ...base, top: belowTop, left };
    }
    if (aboveBottom + 200 <= vp.h) {
      return { ...base, bottom: aboveBottom, left };
    }
    // fall back to centered
    return { ...base, top: "50%", left: "50%", transform: "translate(-50%,-50%)" };
  })();

  const tooltipRef2 = tooltipRef;

  return (
    // Hide on mobile — dashboard is desktop-first
    <div className="hidden md:block" style={{ position: "fixed", inset: 0, zIndex: 9998 }}>
      {spot ? (
        // Spotlight: 4 dimmed strips leaving the anchor area clear
        <>
          <div
            style={{ position: "fixed", inset: `0 0 auto 0`, height: spot.top, background: DIM }}
            onClick={dismiss}
          />
          <div
            style={{
              position: "fixed",
              top: spot.top + spot.height,
              left: 0,
              right: 0,
              bottom: 0,
              background: DIM,
            }}
            onClick={dismiss}
          />
          <div
            style={{
              position: "fixed",
              top: spot.top,
              left: 0,
              width: spot.left,
              height: spot.height,
              background: DIM,
            }}
            onClick={dismiss}
          />
          <div
            style={{
              position: "fixed",
              top: spot.top,
              left: spot.left + spot.width,
              right: 0,
              height: spot.height,
              background: DIM,
            }}
            onClick={dismiss}
          />
          {/* Rounded border ring around the spotlight */}
          <div
            style={{
              position: "fixed",
              top: spot.top,
              left: spot.left,
              width: spot.width,
              height: spot.height,
              borderRadius: 10,
              outline: "2px solid rgba(255,255,255,0.25)",
              pointerEvents: "none",
            }}
          />
        </>
      ) : (
        // No anchor: full-screen dim
        <div
          style={{ position: "fixed", inset: 0, background: DIM }}
          onClick={dismiss}
        />
      )}

      {/* Tooltip card */}
      <div
        ref={tooltipRef2}
        style={tooltipStyle}
        className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-xl"
      >
        <div className="mb-3 flex items-center justify-between">
          <span className="text-xs font-medium text-zinc-400">
            {step + 1} / {STEPS.length}
          </span>
          <button
            type="button"
            onClick={dismiss}
            aria-label="Close tour"
            className="text-zinc-400 transition-colors hover:text-zinc-600"
          >
            ✕
          </button>
        </div>

        <h3 className="mb-1 text-base font-semibold text-zinc-900">
          {current.title}
        </h3>
        <p className="mb-4 text-sm leading-relaxed text-zinc-600">{current.body}</p>

        {current.cta && (
          <a
            href={current.cta.href}
            target="_blank"
            rel="noopener noreferrer"
            className="mb-4 flex items-center justify-center rounded-xl bg-primary-600 px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-primary-700"
          >
            {current.cta.label}
          </a>
        )}

        <div className="flex items-center justify-between gap-2">
          {!isLast ? (
            <button
              type="button"
              onClick={dismiss}
              className="text-xs text-zinc-400 transition-colors hover:text-zinc-600"
            >
              Skip tour
            </button>
          ) : (
            <div />
          )}
          <button
            type="button"
            onClick={next}
            className="rounded-xl bg-primary-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-700"
          >
            {isLast ? "Done" : step === 0 ? "Get started →" : "Next →"}
          </button>
        </div>
      </div>
    </div>
  );
}
