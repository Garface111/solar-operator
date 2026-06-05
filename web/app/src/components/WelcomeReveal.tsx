import { useEffect, useRef, useState } from "react";
import type { ClientRow } from "../lib/api";

type Phase = "idle" | "greeting" | "cascading" | "done";

export type GetItemProps = (
  index: number,
) => React.HTMLAttributes<HTMLDivElement> & { ["data-reveal-index"]?: number };

interface Props {
  clients: ClientRow[] | null;
  operatorName: string | null;
  children: (getItemProps: GetItemProps, revealPhase: Phase) => React.ReactNode;
}

const THROTTLE_KEY = "so_last_reveal";
const THROTTLE_MS = 5 * 60 * 1000; // 5 minutes — short enough to see on demand
const GREETING_MS = 1100;
const CARD_STAGGER_MS = 140;
const CARD_ANIM_MS = 560;

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
      setPhase("greeting");
    }
  }, [clients, phase, freshVisit, forceReveal]);

  // Move from greeting → cascading after the greeting holds.
  useEffect(() => {
    if (phase !== "greeting") return;
    const t = window.setTimeout(() => setPhase("cascading"), GREETING_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  // When cascading: schedule the final-done after the last card lands.
  useEffect(() => {
    if (phase !== "cascading" || !clients) return;
    const total = clients.length * CARD_STAGGER_MS + CARD_ANIM_MS + 200;
    const t = window.setTimeout(() => {
      setPhase("done");
      markRevealed();
    }, total);
    return () => window.clearTimeout(t);
  }, [phase, clients]);

  function skip() {
    if (phase === "done") return;
    setPhase("done");
    markRevealed();
  }

  function getItemProps(
    index: number,
  ): React.HTMLAttributes<HTMLDivElement> & { ["data-reveal-index"]?: number } {
    if (phase === "idle" || phase === "greeting") {
      // hide cards entirely until cascade starts so the greeting owns the stage
      return {
        style: { opacity: 0 } as React.CSSProperties,
      };
    }
    if (phase === "cascading") {
      return {
        className: "so-reveal-card",
        style: {
          animationDelay: `${index * CARD_STAGGER_MS}ms`,
        } as React.CSSProperties,
        "data-reveal-index": index,
      };
    }
    return {};
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
  const showOverlay = phase === "greeting" || phase === "cascading";

  return (
    <div className="relative" onClick={showOverlay ? skip : undefined}>
      {showOverlay && (
        <div
          className="so-reveal-overlay pointer-events-none fixed inset-0 z-30 flex flex-col items-center justify-start pt-24"
          aria-live="polite"
        >
          <div className="so-reveal-greeting text-center">
            <p className="font-serif text-4xl font-semibold tracking-tight text-wood-700 md:text-5xl">
              Welcome back,
              <br />
              <span className="text-emerald-700">{displayName}</span>.
            </p>
            {phase === "cascading" && (
              <p className="so-reveal-subline mt-4 text-sm text-wood-500">
                Loading {totalClients} client{totalClients === 1 ? "" : "s"} ·{" "}
                {totalArrays} array{totalArrays === 1 ? "" : "s"}…
              </p>
            )}
          </div>
        </div>
      )}

      {children(getItemProps, phase)}

      {phase === "done" && totalClients > 0 && (
        <p className="so-welcome-footer mt-4 text-center text-xs text-wood-500">
          {totalClients} client{totalClients === 1 ? "" : "s"} ·{" "}
          {totalArrays} array{totalArrays === 1 ? "" : "s"} · last update{" "}
          {formatAgo(latestSync)} ago
        </p>
      )}
    </div>
  );
}
