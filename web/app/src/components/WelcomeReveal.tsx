import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ClientRow } from "../lib/api";

type Phase = "idle" | "filling" | "done";

export type GetItemProps = (
  index: number,
) => React.HTMLAttributes<HTMLDivElement>;

interface Props {
  clients: ClientRow[] | null;
  operatorName: string | null;
  children: (getItemProps: GetItemProps) => React.ReactNode;
}

interface RevealCtx {
  /** True while numbers should count up. False once locked. */
  active: boolean;
  /** Per-card stagger lookup so child numbers know their delay. */
  delayFor: (cardIndex: number, slot?: number) => number;
}

const Ctx = createContext<RevealCtx>({ active: false, delayFor: () => 0 });

export function useReveal(): RevealCtx {
  return useContext(Ctx);
}

const THROTTLE_KEY = "so_last_reveal";
const THROTTLE_MS = 5 * 60 * 1000;
const CARD_STAGGER_MS = 90;
const NUMBER_BASE_DELAY_MS = 120;

function checkThrottle(): boolean {
  try {
    const ts = localStorage.getItem(THROTTLE_KEY);
    return !!ts && Date.now() - Number(ts) < THROTTLE_MS;
  } catch {
    return false;
  }
}

function markRevealed(): void {
  try {
    localStorage.setItem(THROTTLE_KEY, String(Date.now()));
  } catch {
    /* ignore */
  }
}

/**
 * WelcomeReveal v3 — no overlay theatre.
 *
 * The cards are already there. We just let an AI-style number-fill
 * animation run inside each card: counts tick up from 0, names/dates
 * fade in. The reveal context tells child <RevealNumber> components
 * to play with a staggered delay.
 */
export function WelcomeReveal({ clients, children }: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  const freshVisit = useRef(
    new URLSearchParams(window.location.search).get("fresh") === "1",
  ).current;
  const forceReveal = useRef(
    new URLSearchParams(window.location.search).get("reveal") === "1",
  ).current;

  useEffect(() => {
    if (clients === null || clients.length === 0) return;
    if (phase !== "idle") return;
    if (!freshVisit && !forceReveal && checkThrottle()) {
      setPhase("done");
    } else {
      setPhase("filling");
    }
  }, [clients, phase, freshVisit, forceReveal]);

  // After all numbers have had time to settle, lock the reveal.
  useEffect(() => {
    if (phase !== "filling" || !clients) return;
    // Longest expected delay = stagger * N + number anim duration (~720ms)
    const totalMs = clients.length * CARD_STAGGER_MS + 1100;
    const t = window.setTimeout(() => {
      setPhase("done");
      markRevealed();
    }, totalMs);
    return () => window.clearTimeout(t);
  }, [phase, clients]);

  function getItemProps(index: number): React.HTMLAttributes<HTMLDivElement> {
    if (phase === "filling") {
      return {
        className: "so-reveal-card",
        style: {
          animationDelay: `${index * CARD_STAGGER_MS}ms`,
        } as React.CSSProperties,
      };
    }
    return {};
  }

  const ctx = useMemo<RevealCtx>(
    () => ({
      active: phase === "filling",
      delayFor: (cardIndex: number, slot = 0) =>
        cardIndex * CARD_STAGGER_MS + NUMBER_BASE_DELAY_MS + slot * 80,
    }),
    [phase],
  );

  return <Ctx.Provider value={ctx}>{children(getItemProps)}</Ctx.Provider>;
}
