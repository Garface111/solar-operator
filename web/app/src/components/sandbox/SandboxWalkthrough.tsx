import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';

// TEST SEAM: exported so unit tests can assert on the key value directly.
export const LS_KEY = 'so:walkthrough:sandbox-v2:done';

type Step = 'welcome' | 'cta' | 'captured' | 'loop' | 'done';

interface ElemPos {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

interface Props {
  clientCount: number;
  lastCapturedClientId: number | null;
  onOpenByLogin: () => void;
  onOpenManual: () => void;
}

// TEST SEAM: exported so unit tests can exercise the step-selection logic in isolation.
export function initStep(clientCount: number): Step {
  if (localStorage.getItem(LS_KEY) === 'true') return 'done';
  if (clientCount >= 3) return 'done';
  if (clientCount >= 2) return 'loop';
  if (clientCount === 1) return 'welcome';
  return 'done';
}

// rAF loop tracks a DOM element's position relative to the overlay div.
// Updates state only when position changes meaningfully (1px threshold).
function useElemPos(
  overlayRef: RefObject<HTMLDivElement>,
  selector: string | null,
  active: boolean,
): ElemPos | null {
  const [pos, setPos] = useState<ElemPos | null>(null);

  useEffect(() => {
    if (!active || !selector) {
      setPos(null);
      return;
    }
    let rafId: number;
    let lastKey = '';

    const tick = () => {
      const overlay = overlayRef.current;
      const el = document.querySelector(selector);
      if (overlay && el) {
        const or = overlay.getBoundingClientRect();
        const er = el.getBoundingClientRect();
        const next: ElemPos = {
          top: er.top - or.top,
          left: er.left - or.left,
          right: er.right - or.left,
          bottom: er.bottom - or.top,
          width: er.width,
          height: er.height,
        };
        const key = `${Math.round(next.top)},${Math.round(next.left)},${Math.round(next.right)}`;
        if (key !== lastKey) {
          lastKey = key;
          setPos(next);
        }
      }
      rafId = requestAnimationFrame(tick);
    };

    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [overlayRef, selector, active]);

  return pos;
}

// SVG curved arrow pointing leftward — tip at left, tail at right.
function ArrowLeft({ className = '' }: { className?: string }) {
  return (
    <svg width="40" height="24" viewBox="0 0 40 24" fill="none" aria-hidden="true" className={className}>
      <path d="M38 12 C 26 12 14 12 4 12" stroke="#047857" strokeWidth="2.5" strokeLinecap="round" />
      <path d="M 4 12 L 13 5" stroke="#047857" strokeWidth="2.5" strokeLinecap="round" />
      <path d="M 4 12 L 13 19" stroke="#047857" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

// SVG curved arrow pointing upward — tip at top, tail at bottom.
function ArrowUp({ className = '' }: { className?: string }) {
  return (
    <svg width="24" height="40" viewBox="0 0 24 40" fill="none" aria-hidden="true" className={className}>
      <path d="M12 38 C 12 26 12 14 12 4" stroke="#047857" strokeWidth="2.5" strokeLinecap="round" />
      <path d="M 12 4 L 5 13" stroke="#047857" strokeWidth="2.5" strokeLinecap="round" />
      <path d="M 12 4 L 19 13" stroke="#047857" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}

function CalloutCard({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-xl bg-white px-4 py-3"
      style={{
        width: 272,
        boxShadow: '0 4px 24px -4px rgba(0,0,0,0.18), 0 0 0 1px rgba(0,0,0,0.06)',
        pointerEvents: 'auto',
      }}
    >
      {children}
    </div>
  );
}

export function SandboxWalkthrough({
  clientCount,
  lastCapturedClientId,
  onOpenByLogin,
  onOpenManual,
}: Props) {
  const [step, setStep] = useState<Step>(() => initStep(clientCount));
  const [fadeIn, setFadeIn] = useState(false);
  const overlayRef = useRef<HTMLDivElement>(null);
  const prevCapturedRef = useRef<number | null>(null);

  const go = useCallback((next: Step) => {
    setFadeIn(false);
    setTimeout(() => {
      setStep(next);
      // Double rAF ensures the DOM renders the new step content before we fade in
      requestAnimationFrame(() => requestAnimationFrame(() => setFadeIn(true)));
    }, 200);
  }, []);

  const markDone = useCallback(() => {
    localStorage.setItem(LS_KEY, 'true');
    go('done');
  }, [go]);

  // Fade in on first mount
  useEffect(() => {
    if (step === 'done') return;
    const t = setTimeout(() => {
      requestAnimationFrame(() => requestAnimationFrame(() => setFadeIn(true)));
    }, 150);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // intentionally only on mount

  // welcome → cta after a beat
  useEffect(() => {
    if (step !== 'welcome') return;
    const t = setTimeout(() => go('cta'), 1500);
    return () => clearTimeout(t);
  }, [step, go]);

  // cta/welcome → captured when a portal login is captured
  useEffect(() => {
    if (step !== 'cta' && step !== 'welcome') return;
    if (lastCapturedClientId != null && lastCapturedClientId !== prevCapturedRef.current) {
      prevCapturedRef.current = lastCapturedClientId;
      go('captured');
    }
  }, [lastCapturedClientId, step, go]);

  // cta/welcome → loop if client was added manually (no login capture)
  useEffect(() => {
    if (step !== 'cta' && step !== 'welcome') return;
    if (clientCount >= 2 && lastCapturedClientId == null) go('loop');
  }, [clientCount, step, lastCapturedClientId, go]);

  // captured → loop after callout lingers
  useEffect(() => {
    if (step !== 'captured') return;
    const t = setTimeout(() => go('loop'), 4000);
    return () => clearTimeout(t);
  }, [step, go]);

  // Auto-complete at 3+ clients
  useEffect(() => {
    if (step === 'done') return;
    if (clientCount >= 3) markDone();
  }, [clientCount, step, markDone]);

  // Selectors for DOM-tracked elements
  const capturedCardSel =
    lastCapturedClientId != null
      ? `[data-walkthrough-client-id="client_${lastCapturedClientId}"]`
      : null;
  const capturedLoginSel =
    lastCapturedClientId != null
      ? `[data-walkthrough-client-id="client_${lastCapturedClientId}"] [data-walkthrough="login-row"]`
      : null;

  const firstCardPos = useElemPos(overlayRef, '[data-walkthrough="client-card"]', step === 'welcome');
  const addBtnPos = useElemPos(overlayRef, '[data-walkthrough="add-client-btn"]', step === 'cta');
  const capturedLoginPos = useElemPos(overlayRef, capturedLoginSel, step === 'captured');
  const capturedCardPos = useElemPos(
    overlayRef,
    capturedCardSel,
    step === 'captured' && capturedLoginPos == null,
  );

  if (step === 'done') return null;

  const capturedTarget = capturedLoginPos ?? capturedCardPos;

  // Horizontal center of the add-client button (for arrow alignment)
  const btnCenterX = addBtnPos ? addBtnPos.left + addBtnPos.width / 2 : 0;
  // Callout left: aligned right of button center, clamped from edges
  const ctaCalloutLeft = addBtnPos
    ? Math.max(8, Math.min(btnCenterX - 136, addBtnPos.right - 272))
    : 0;
  // Arrow horizontal offset within the callout so it points at the button center
  const arrowOffsetLeft = addBtnPos ? Math.max(4, Math.min(btnCenterX - ctaCalloutLeft - 12, 248)) : 110;

  return (
    <>
      {/* Animation keyframes — scoped to this walkthrough layer */}
      <style>{`
        @keyframes wt-left { 0%,100%{transform:translateX(0)} 50%{transform:translateX(-6px)} }
        @keyframes wt-up   { 0%,100%{transform:translateY(0)} 50%{transform:translateY(-6px)} }
        @keyframes wt-pulse {
          0%,100%{box-shadow:0 0 0 3px rgba(4,120,87,.15)}
          50%    {box-shadow:0 0 0 7px rgba(4,120,87,.05)}
        }
        .wt-bounce-left { animation: wt-left 1.7s ease-in-out infinite; }
        .wt-bounce-up   { animation: wt-up   1.7s ease-in-out infinite; }
        .wt-pulse       { animation: wt-pulse 2s ease-in-out infinite; }
      `}</style>

      <div
        ref={overlayRef}
        className="absolute inset-0 overflow-hidden"
        style={{ zIndex: 30, pointerEvents: 'none' }}
      >
        {/* ── Crossfading step content ─────────────────────────────────── */}
        <div style={{ opacity: fadeIn ? 1 : 0, transition: 'opacity 0.2s ease' }}>

          {/* STEP: welcome — callout right of first client card */}
          {step === 'welcome' && firstCardPos && (
            <div
              style={{
                position: 'absolute',
                top: Math.max(8, firstCardPos.top + firstCardPos.height / 2 - 52),
                left: firstCardPos.right + 8,
                display: 'flex',
                alignItems: 'center',
                gap: 4,
              }}
            >
              <ArrowLeft className="wt-bounce-left shrink-0" />
              <CalloutCard>
                <p className="mb-1 text-[13px] font-semibold text-zinc-900">
                  This is your first client.
                </p>
                <p className="text-xs leading-relaxed text-zinc-500">
                  Each client owns one or more utility logins. Their solar arrays and accounts live here.
                </p>
              </CalloutCard>
            </div>
          )}

          {/* STEP: cta — callout below add-client button */}
          {step === 'cta' && addBtnPos && (
            <div
              style={{
                position: 'absolute',
                top: addBtnPos.bottom + 6,
                left: ctaCalloutLeft,
              }}
            >
              {/* Arrow centered on the toolbar button */}
              <div style={{ marginBottom: 4, paddingLeft: arrowOffsetLeft }}>
                <ArrowUp className="wt-bounce-up" />
              </div>
              <CalloutCard>
                <p className="mb-1 text-[13px] font-semibold text-zinc-900">
                  Add your next client
                </p>
                <p className="mb-3 text-xs leading-relaxed text-zinc-500">
                  Sign into their GMP portal and we'll capture their accounts automatically.
                </p>
                <button
                  type="button"
                  className="mb-2 w-full rounded-lg bg-primary-500 px-3 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-600 active:bg-primary-700"
                  onClick={() => { onOpenByLogin(); markDone(); }}
                  style={{ pointerEvents: 'auto' }}
                >
                  Connect a GMP login
                </button>
                <button
                  type="button"
                  className="w-full rounded-lg border border-zinc-200 px-3 py-1.5 text-xs text-zinc-500 transition-colors hover:bg-zinc-50"
                  onClick={() => { onOpenManual(); markDone(); }}
                  style={{ pointerEvents: 'auto' }}
                >
                  + Add empty client
                </button>
              </CalloutCard>
            </div>
          )}

          {/* STEP: captured — glow + callout pointing at new login row */}
          {step === 'captured' && capturedTarget && (
            <>
              {capturedLoginPos && (
                <div
                  className="wt-pulse"
                  style={{
                    position: 'absolute',
                    top: capturedLoginPos.top - 2,
                    left: capturedLoginPos.left - 4,
                    width: capturedLoginPos.width + 8,
                    height: capturedLoginPos.height + 4,
                    borderRadius: 10,
                    background: 'rgba(4,120,87,0.06)',
                    border: '1.5px solid rgba(4,120,87,0.22)',
                    pointerEvents: 'none',
                  }}
                />
              )}
              <div
                style={{
                  position: 'absolute',
                  top: Math.max(8, capturedTarget.top + capturedTarget.height / 2 - 48),
                  left: capturedTarget.right + 8,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                }}
              >
                <ArrowLeft className="wt-bounce-left shrink-0" />
                <CalloutCard>
                  <p className="mb-1 text-[13px] font-semibold text-zinc-900">
                    New login captured!
                  </p>
                  <p className="text-xs leading-relaxed text-zinc-500">
                    Drag this row to a different client if it belongs there.
                  </p>
                </CalloutCard>
              </div>
            </>
          )}

          {/* Persistent corner buttons — shown during captured + loop */}
          {(step === 'loop' || step === 'captured') && (
            <div
              className="absolute flex flex-col items-end gap-2"
              style={{ bottom: 52, right: 24, pointerEvents: 'auto' }}
            >
              <button
                type="button"
                className="rounded-xl bg-primary-500 px-4 py-2.5 text-sm font-semibold text-white shadow-md transition-colors hover:bg-primary-600 active:bg-primary-700"
                onClick={() => { onOpenByLogin(); markDone(); }}
              >
                + Add another client
              </button>
              <button
                type="button"
                className="rounded-xl border border-zinc-200 bg-white px-4 py-2 text-xs font-medium text-zinc-600 shadow-sm transition-colors hover:bg-zinc-50"
                onClick={() => { onOpenManual(); markDone(); }}
              >
                + Add empty client
              </button>
            </div>
          )}
        </div>

        {/* Skip link — always visible throughout walkthrough */}
        <div
          className="absolute bottom-6 right-6"
          style={{ pointerEvents: 'auto' }}
        >
          <button
            type="button"
            className="text-xs text-zinc-400 underline underline-offset-2 transition-colors hover:text-zinc-600"
            onClick={markDone}
          >
            Skip walkthrough
          </button>
        </div>
      </div>
    </>
  );
}
