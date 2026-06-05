import { useCallback, useEffect, useRef, useState } from "react";
import { markWalkthroughSeen } from "../lib/walkthrough";
import { openPortalTab } from "../lib/openPortalTab";

interface SpotRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

const PAD = 10;
const RADIUS = 14;
const DIM = "rgba(0,0,0,0.55)";

interface StepDef {
  anchor: string | null;
  title: string;
  body: string;
  /** When true the tour does not advance until the operator clicks the
   *  spotlighted element. Used for steps that teach a click. */
  waitForClick?: boolean;
  cta?: { label: string; href: string };
}

function makeSteps(hasCaptures: boolean): StepDef[] {
  return [
  {
    anchor: null,
    title: "Welcome to your dashboard",
    body: hasCaptures
      ? "Your first utility login is already captured — we auto-created a client for it and attached its arrays. From here you'll manage clients, add NEPOOL IDs, and watch reports queue up. 60-second tour?"
      : "Your dashboard is ready. Sign into your utility portal to capture your first client — we'll show you how. 60-second tour?",
  },
  {
    anchor: "2",
    title: "Each card is a client",
    body: "We named this one from the utility account we just captured. Click the name to rename it to whatever you'd put on a report — your operator name, the LLC, whatever you'd say to a client over coffee.",
  },
  {
    anchor: "7",
    title: "Arrays live under each client",
    body: "These are the meters we pulled from the utility portal. Each row is one billable array. The bill cycle, kWh, and NEPOOL ID columns fill in as data lands — nothing for you to type.",
  },
  {
    anchor: "5",
    title: "Add NEPOOL IDs in one shot",
    body: "When you have a spreadsheet of array names + NEPOOL-GIS IDs, drop it here. We fuzzy-match rows to the arrays we already pulled and stamp the IDs in. There's also a per-client version inside each card.",
  },
  {
    anchor: "6-add",
    title: "Add more clients later",
    body: "Two ways to grow: (1) sign into another utility account from your portal — we'll auto-create a client for that login too. (2) Click + Add client to type one in by hand. Whichever's faster.",
  },
  {
    anchor: "6-import",
    title: "Or import a roster spreadsheet",
    body: "If you already have a spreadsheet of clients + their arrays + NEPOOL IDs, drop the whole file here and we'll seed everything in one shot. Useful when you're inheriting an existing book of business.",
  },
  {
    anchor: null,
    title: "You're set",
    body: "From here it runs itself. Reports auto-generate on your selected cadence. We email you a sample after the first quarter so you can review the format. Holler if anything looks off.",
  },
  ];
}

interface Props {
  onClose: () => void;
  hasCaptures: boolean;
}

export function WalkthroughOverlay({ onClose, hasCaptures }: Props) {
  const STEPS = makeSteps(hasCaptures);
  const [step, setStep] = useState(0);
  const [spot, setSpot] = useState<SpotRect | null>(null);
  const [viewport, setViewport] = useState({ w: 0, h: 0 });
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
    setViewport({ w: window.innerWidth, h: window.innerHeight });
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
    // Re-measure after layout settles (expanding a card animates).
    const id1 = window.setTimeout(updateSpot, 100);
    const id2 = window.setTimeout(updateSpot, 400);
    window.addEventListener("scroll", updateSpot, { passive: true });
    window.addEventListener("resize", updateSpot);
    return () => {
      window.clearTimeout(id1);
      window.clearTimeout(id2);
      window.removeEventListener("scroll", updateSpot);
      window.removeEventListener("resize", updateSpot);
    };
  }, [updateSpot]);

  // When a waitForClick step is active, listen for clicks INSIDE the anchored
  // element and advance the step. Click outside dismisses (unless it's on the
  // tooltip itself).
  useEffect(() => {
    if (!current.waitForClick || !current.anchor) return;
    function onDocClick(e: MouseEvent) {
      const anchorEl = document.querySelector<HTMLElement>(
        `[data-tour-step="${current.anchor}"]`,
      );
      if (!anchorEl) return;
      const target = e.target as Node;
      const tooltip = tooltipRef.current;
      if (anchorEl.contains(target)) {
        // Let the click pass through to the underlying control; advance after a
        // tick so any state updates from that click land first.
        window.setTimeout(() => setStep((s) => Math.min(s + 1, STEPS.length - 1)), 50);
      } else if (tooltip && tooltip.contains(target)) {
        // ignore — tooltip clicks handled by its own buttons
      }
      // Clicks on the dimmed mask are intentionally ignored during
      // waitForClick steps so the operator can't dismiss-by-accident
      // while reaching for the highlighted control.
    }
    document.addEventListener("click", onDocClick, true);
    return () => document.removeEventListener("click", onDocClick, true);
  }, [current.waitForClick, current.anchor]);

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
    return { ...base, top: "50%", left: "50%", transform: "translate(-50%,-50%)" };
  })();

  // Mask rendered as a single SVG with a rect-with-rounded-cutout so the
  // spotlight has truly rounded corners (the old 4-strip approach gave
  // sharp corners around the highlighted element).
  const w = viewport.w || (typeof window !== "undefined" ? window.innerWidth : 1280);
  const h = viewport.h || (typeof window !== "undefined" ? window.innerHeight : 720);

  return (
    // Hide on mobile — dashboard is desktop-first
    <div className="hidden md:block" style={{ position: "fixed", inset: 0, zIndex: 9998 }}>
      {/* SVG mask: full-screen dim with a rounded cutout around the anchor.
          pointerEvents on the SVG = none lets clicks pass through to the
          spotlighted UI; the dim rect catches clicks via its own onClick to
          dismiss the tour when the operator clicks an unrelated area
          (skipped when waitForClick is active to prevent accidental
          dismissal). */}
      <svg
        width={w}
        height={h}
        style={{ position: "fixed", inset: 0, pointerEvents: "none" }}
      >
        <defs>
          <mask id="walkthrough-mask">
            <rect x={0} y={0} width={w} height={h} fill="white" />
            {spot && (
              <rect
                x={spot.left}
                y={spot.top}
                width={spot.width}
                height={spot.height}
                rx={RADIUS}
                ry={RADIUS}
                fill="black"
              />
            )}
          </mask>
        </defs>
        <rect
          x={0}
          y={0}
          width={w}
          height={h}
          fill={DIM}
          mask="url(#walkthrough-mask)"
          style={{ pointerEvents: current.waitForClick ? "none" : "auto", cursor: current.waitForClick ? "default" : "pointer" }}
          onClick={current.waitForClick ? undefined : dismiss}
        />
        {spot && (
          // Rounded border ring around the spotlight
          <rect
            x={spot.left}
            y={spot.top}
            width={spot.width}
            height={spot.height}
            rx={RADIUS}
            ry={RADIUS}
            fill="none"
            stroke="rgba(255,255,255,0.35)"
            strokeWidth={2}
          />
        )}
      </svg>

      {/* Tooltip card */}
      <div
        ref={tooltipRef}
        style={tooltipStyle}
        className="rounded-2xl border border-cream-border bg-cream p-5 shadow-xl"
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
            onClick={(e) => {
              // Prefer the extension's background-tab path so the operator
              // keeps watching the dashboard while data lands. Fall back to
              // a normal new tab if the extension isn't installed.
              e.preventDefault();
              void openPortalTab(current.cta!.href);
            }}
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
          {current.waitForClick ? (
            <span className="rounded-xl bg-zinc-100 px-4 py-2 text-xs font-medium text-zinc-500">
              Click the highlighted area to continue
            </span>
          ) : (
            <button
              type="button"
              onClick={next}
              className="rounded-xl bg-primary-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-700"
            >
              {isLast ? "Done" : step === 0 ? "Get started →" : "Next →"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
