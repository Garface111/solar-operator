import { useEffect, useRef, useState } from "react";
import type { ClientRow } from "../lib/api";

type Phase = "idle" | "animating" | "done";

export type GetItemProps = (
  index: number,
) => React.HTMLAttributes<HTMLDivElement>;

interface Props {
  clients: ClientRow[] | null;
  operatorName: string | null;
  children: (getItemProps: GetItemProps) => React.ReactNode;
}

const THROTTLE_KEY = "so_last_reveal";
const THROTTLE_MS = 2 * 60 * 60 * 1000; // 2 hours
const GREETING_MS = 700;
const CARD_STAGGER_MS = 80;

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
    // storage quota — ignore
  }
}

function formatAgo(isoStr: string | null): string {
  if (!isoStr) return "never";
  const diffMs = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function WelcomeReveal({ clients, operatorName, children }: Props) {
  const [phase, setPhase] = useState<Phase>("idle");
  // Stable across renders — read once on mount.
  const freshVisit = useRef(
    new URLSearchParams(window.location.search).get("fresh") === "1",
  ).current;

  useEffect(() => {
    if (clients === null || clients.length === 0) return;
    if (phase !== "idle") return;

    if (!freshVisit && checkThrottle()) {
      setPhase("done");
    } else {
      setPhase("animating");
    }
  }, [clients, phase, freshVisit]);

  function skip() {
    if (phase !== "animating") return;
    setPhase("done");
    markRevealed();
  }

  function getItemProps(index: number): React.HTMLAttributes<HTMLDivElement> {
    if (phase !== "animating") return {};
    const isLast = index === (clients?.length ?? 1) - 1;
    return {
      className: "so-cascade-row",
      style: {
        animationDelay: `${GREETING_MS + index * CARD_STAGGER_MS}ms`,
      } as React.CSSProperties,
      ...(isLast
        ? {
            onAnimationEnd: (e: React.AnimationEvent<HTMLDivElement>) => {
              // Guard against bubbled events from children.
              if (e.animationName === "so-cascade-row-in") {
                setPhase("done");
                markRevealed();
              }
            },
          }
        : {}),
    };
  }

  const totalClients = clients?.length ?? 0;
  const totalArrays =
    clients?.reduce((s, c) => s + c.array_count, 0) ?? 0;

  const latestSync = (() => {
    const sorted = clients
      ?.flatMap((c) => [c.gmp_last_sync_at, c.vec_last_sync_at])
      .filter((d): d is string => d !== null)
      .sort();
    return sorted && sorted.length > 0 ? sorted[sorted.length - 1] : null;
  })();

  const displayName = operatorName ?? "there";

  return (
    // Clicking anywhere during the animation skips to the final state.
    <div
      className="relative"
      onClick={phase === "animating" ? skip : undefined}
    >
      {phase === "animating" && (
        <div
          className="so-welcome-greeting pointer-events-none absolute left-0 right-0 top-0 z-10"
          aria-live="polite"
        >
          <p className="font-serif text-2xl font-bold text-wood-600">
            Welcome back, {displayName}.
          </p>
        </div>
      )}

      {children(getItemProps)}

      {phase === "done" && totalClients > 0 && (
        <p className="so-welcome-footer mt-3 text-center text-xs text-wood-500">
          {totalClients} client{totalClients === 1 ? "" : "s"} ·{" "}
          {totalArrays} array{totalArrays === 1 ? "" : "s"} · last update{" "}
          {formatAgo(latestSync)} ago
        </p>
      )}
    </div>
  );
}
