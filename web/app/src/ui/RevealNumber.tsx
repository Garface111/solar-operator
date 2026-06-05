import { useEffect, useRef, useState } from "react";

interface Props {
  /** Final target number. */
  value: number;
  /** Decimal places. 0 = integer. */
  decimals?: number;
  /** Animation duration in ms. */
  durationMs?: number;
  /** Delay before counting starts. */
  delayMs?: number;
  /** Suffix appended after the number (e.g. " MWh", "%"). */
  suffix?: string;
  /** Prefix prepended before the number (e.g. "$"). */
  prefix?: string;
  /** When true, skip animation and show final value immediately. */
  instant?: boolean;
  className?: string;
}

/**
 * RevealNumber — counts up from 0 to `value` with eased tween, then locks.
 * The "AI is filling this box in" feeling. Sub-second by default; subtle.
 *
 * Respects prefers-reduced-motion by snapping to final value.
 * One-shot: replays only when `value` changes from one stable number to
 * another (debounced via the effect dependency).
 */
export function RevealNumber({
  value,
  decimals = 0,
  durationMs = 720,
  delayMs = 0,
  suffix = "",
  prefix = "",
  instant = false,
  className,
}: Props) {
  const [displayed, setDisplayed] = useState<number>(instant ? value : 0);
  const rafRef = useRef<number | null>(null);
  const startRef = useRef<number | null>(null);

  useEffect(() => {
    if (instant) {
      setDisplayed(value);
      return;
    }
    // Reduced motion — skip the animation.
    if (
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      setDisplayed(value);
      return;
    }

    const from = 0;
    const to = value;
    startRef.current = null;

    const startedAt = performance.now() + delayMs;

    function frame(now: number) {
      if (now < startedAt) {
        rafRef.current = requestAnimationFrame(frame);
        return;
      }
      const t = Math.min(1, (now - startedAt) / durationMs);
      // easeOutCubic — fast start, gentle settle
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplayed(from + (to - from) * eased);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(frame);
      } else {
        setDisplayed(to);
        rafRef.current = null;
      }
    }

    rafRef.current = requestAnimationFrame(frame);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [value, durationMs, delayMs, instant]);

  const text =
    decimals === 0
      ? Math.round(displayed).toLocaleString()
      : displayed.toLocaleString(undefined, {
          minimumFractionDigits: decimals,
          maximumFractionDigits: decimals,
        });

  return (
    <span className={className}>
      {prefix}
      {text}
      {suffix}
    </span>
  );
}
