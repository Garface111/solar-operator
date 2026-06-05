import { useEffect, useRef, useState } from "react";

interface Props {
  /** Final target number. */
  value: number;
  /** Decimal places. 0 = integer. */
  decimals?: number;
  /** Animation duration in ms. Default 2200 — slow + visible. */
  durationMs?: number;
  /** Delay before counting starts. */
  delayMs?: number;
  /** Suffix appended after the number. */
  suffix?: string;
  /** Prefix prepended before the number. */
  prefix?: string;
  /** When true, skip animation and show final value immediately. */
  instant?: boolean;
  className?: string;
}

/**
 * RevealNumber — counts up from 0 to `value` with eased tween while
 * pulsing emerald so the user clearly sees an AI filling the box.
 *
 * On final value: snaps to zinc and lands. Sub-2s by default.
 * Respects prefers-reduced-motion by snapping to final value.
 */
export function RevealNumber({
  value,
  decimals = 0,
  durationMs = 2200,
  delayMs = 0,
  suffix = "",
  prefix = "",
  instant = false,
  className,
}: Props) {
  const [displayed, setDisplayed] = useState<number>(instant ? value : 0);
  const [animating, setAnimating] = useState<boolean>(!instant);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    if (instant) {
      setDisplayed(value);
      setAnimating(false);
      return;
    }
    if (
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      setDisplayed(value);
      setAnimating(false);
      return;
    }

    setDisplayed(0);
    setAnimating(true);

    const startedAt = performance.now() + delayMs;

    function frame(now: number) {
      if (now < startedAt) {
        rafRef.current = requestAnimationFrame(frame);
        return;
      }
      const t = Math.min(1, (now - startedAt) / durationMs);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - t, 3);
      setDisplayed(value * eased);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(frame);
      } else {
        setDisplayed(value);
        setAnimating(false);
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

  // While animating: emerald + faint glow + slight scale. On done: snap to
  // inherited color (the number "lands" into the layout's natural tone).
  const animClass = animating
    ? "font-bold text-emerald-600 transition-colors duration-300 [text-shadow:0_0_12px_rgba(16,185,129,0.55)]"
    : "transition-colors duration-300";

  return (
    <span className={`${animClass} ${className ?? ""}`.trim()}>
      {prefix}
      {text}
      {suffix}
      {animating && (
        <span
          aria-hidden
          className="ml-0.5 inline-block w-[2px] h-[1em] align-text-bottom bg-emerald-500 animate-pulse"
        />
      )}
    </span>
  );
}
