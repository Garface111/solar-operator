import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";
import { ClientsSection } from "../components/ClientsSection";
import SandboxCanvas from "../components/sandbox/SandboxCanvas";

/**
 * Clients tab — ONE page (Ford 2026-07-24): the table on top, the spatial
 * sandbox directly below it, filling what used to be dead space. The old
 * Table/Sandbox pill toggle is gone — "find one client and edit one field"
 * happens in the list, "where am I, what do I have" lives in the canvas,
 * and you see both without choosing.
 *
 * The canvas keeps its own fullscreen toggle (fixed overlay) for real spatial
 * work; Esc exits it.
 */
export default function ClientsTab() {
  // Deep links to a specific client: /clients/:clientId auto-expands the
  // list-view card. The canvas autopans to the same client on load.
  const { clientId } = useParams();

  // CSS-based fullscreen — the canvas keeps its React tree (ReactFlow state,
  // walkthrough, undo stack) and only the wrapper classes change, so toggling
  // never remounts SandboxCanvas.
  const [isFullscreen, setIsFullscreen] = useState(false);
  const toggleFullscreen = useCallback(() => setIsFullscreen((v) => !v), []);

  // Lock body scroll while the overlay covers the viewport; restore on exit.
  useEffect(() => {
    if (!isFullscreen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [isFullscreen]);

  // Esc exits fullscreen — but only if nothing else already handled it. The
  // canvas's modal/palette/context-menu Esc consumers call preventDefault when
  // they close something, and we skip events targeting inputs (inline renames),
  // so we never steal Esc from an open dialog.
  useEffect(() => {
    if (!isFullscreen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (e.target instanceof HTMLElement) {
        const tag = e.target.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || e.target.isContentEditable) return;
      }
      setIsFullscreen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isFullscreen]);

  return (
    <div className="space-y-4">
      {/* Spreadsheet / list — classic centered column, always visible. */}
      <div className="mx-auto w-full max-w-4xl">
        <ClientsSection expandClientId={clientId ? Number(clientId) : undefined} />
      </div>

      {/* Spatial canvas — full-bleed BELOW the table, always visible on sm+.
          Fullscreen = fixed overlay. It mounts visible now, so ReactFlow
          measures real geometry on first paint (no display:none races). */}
      <section
        aria-label="Clients sandbox"
        className={[
          // NOTE: `relative` and `fixed` must never coexist on this element —
          // Tailwind resolves conflicts by stylesheet order (not class order),
          // and `relative` beats `fixed`, collapsing the section to 0 height.
          "overflow-hidden border border-zinc-200 bg-zinc-50 shadow-sm",
          isFullscreen
            ? "fixed inset-0 z-[100]"
            : "relative h-[calc(100dvh-10rem)] min-h-[26rem] w-full rounded-2xl",
        ].join(" ")}
      >
        {/* Mobile notice — overlays the canvas below 640px. The canvas still
            mounts so ReactFlow doesn't re-initialize on viewport resize. */}
        <div
          aria-hidden
          className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 bg-zinc-50/97 sm:hidden"
        >
          <svg
            width="32"
            height="32"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-zinc-400"
            aria-hidden
          >
            <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
            <line x1="8" y1="21" x2="16" y2="21" />
            <line x1="12" y1="17" x2="12" y2="21" />
          </svg>
          <p className="text-sm font-medium text-zinc-500">
            Sandbox works best on a wider screen.
          </p>
          <p className="text-xs text-zinc-400">
            Scroll up for the client list.
          </p>
        </div>

        <ReactFlowProvider>
          <SandboxCanvas
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
          />
        </ReactFlowProvider>
      </section>
    </div>
  );
}
