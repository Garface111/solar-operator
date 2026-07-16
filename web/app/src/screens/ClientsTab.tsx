import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";
import { ClientsSection } from "../components/ClientsSection";
import SandboxCanvas from "../components/sandbox/SandboxCanvas";

/**
 * Clients tab — the spatial sandbox sits on top, the list view below.
 *
 * Bruce wants the canvas to be THE interaction, not a separate destination.
 * The list stays as the dense reference view underneath: "where am I, what
 * do I have, what's it worth" lives in the canvas; "find one client and edit
 * one field" stays cheaper in the list. Same data, two views, one tab.
 */
export default function ClientsTab() {
  // Supports deep links to a specific client: /clients/:clientId auto-expands
  // the list-view card. The canvas autopans to the same client on load.
  const { clientId } = useParams();

  // CSS-based fullscreen — the canvas keeps its React tree (ReactFlow state,
  // walkthrough, undo stack) and only the wrapper classes change, so toggling
  // never remounts SandboxCanvas. State lives here because the wrapper does.
  const [isFullscreen, setIsFullscreen] = useState(false);
  const toggleFullscreen = useCallback(() => setIsFullscreen((v) => !v), []);

  // Sub-tab: Table (default, left) vs Sandbox (optional spatial view).
  // Both stay MOUNTED (hidden, never unmounted) so the canvas keeps its
  // ReactFlow / undo / walkthrough state across switches. Persisted.
  // Ford 2026-07-15: Table is the default; Sandbox is opt-in.
  const [subtab, setSubtab] = useState<"sandbox" | "spreadsheet">(() => {
    try {
      const v = localStorage.getItem("so:clients:subtab");
      if (v === "sandbox") return "sandbox";
      return "spreadsheet";
    } catch {
      return "spreadsheet";
    }
  });
  const selectSubtab = useCallback((v: "sandbox" | "spreadsheet") => {
    setSubtab(v);
    try { localStorage.setItem("so:clients:subtab", v); } catch { /* ignore */ }
    // The canvas was display:none (zero-size) while hidden — nudge ReactFlow to
    // remeasure on re-show so it doesn't paint into a collapsed box.
    if (v === "sandbox") setTimeout(() => window.dispatchEvent(new Event("resize")), 60);
  }, []);

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

  // Sandbox fills under top nav + compact Table/Sandbox pill. Table keeps the
  // classic max-w-4xl gutters (dead space L/R) — only sandbox is full-bleed.
  // ~3.5rem TabBar + ~0.75rem main pad + ~1.75rem pill + gaps ≈ 6.25rem.
  const sandboxFillClass =
    "relative rounded-2xl h-[calc(100dvh-6.25rem)] min-h-[28rem] w-full";
  const isSandbox = subtab === "sandbox";

  // Let DashboardLayout hide the site footer only while the large sandbox is up.
  useEffect(() => {
    const on = isSandbox && !isFullscreen;
    document.body.classList.toggle("so-clients-sandbox", on);
    return () => document.body.classList.remove("so-clients-sandbox");
  }, [isSandbox, isFullscreen]);

  return (
    <div
      className={
        isSandbox && !isFullscreen ? "flex flex-col gap-3" : "space-y-4"
      }
    >
      {/* Compact pill toggle — sits under the top tabs, not a full-width bar. */}
      {!isFullscreen && (
        <div className="flex w-full shrink-0 justify-center">
          <div
            role="tablist"
            aria-label="Clients view"
            className="inline-flex items-center gap-0.5 rounded-full border border-zinc-200/90 bg-white/95 p-0.5 shadow-sm backdrop-blur-sm"
          >
            {(["spreadsheet", "sandbox"] as const).map((v) => (
              <button
                key={v}
                type="button"
                role="tab"
                aria-selected={subtab === v}
                onClick={() => selectSubtab(v)}
                className={[
                  "rounded-full px-3 py-1 text-[11px] font-semibold leading-none tracking-tight transition-colors",
                  subtab === v
                    ? "bg-primary-500 text-white shadow-sm"
                    : "text-zinc-500 hover:bg-zinc-50 hover:text-zinc-800",
                ].join(" ")}
              >
                {v === "spreadsheet" ? "Table" : "Sandbox"}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Spatial canvas — full-bleed under the top bars. Fullscreen = fixed overlay. */}
      <section
        aria-label="Clients sandbox"
        className={[
          // NOTE: `relative` and `fixed` must never coexist on this element —
          // Tailwind resolves conflicts by stylesheet order (not class order),
          // and `relative` beats `fixed`, collapsing the section to 0 height.
          "overflow-hidden border border-zinc-200 bg-zinc-50 shadow-sm",
          // Sub-tab: hide (don't unmount) when the Spreadsheet view is active.
          isSandbox || isFullscreen ? "" : "hidden",
          isFullscreen ? "fixed inset-0 z-[100]" : sandboxFillClass,
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
            Scroll down for the client list.
          </p>
        </div>

        <ReactFlowProvider>
          <SandboxCanvas
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
          />
        </ReactFlowProvider>
      </section>

      {/* Spreadsheet / list — classic centered column with L/R deadspace
          (max-w-4xl), matching Account / Automatic Reports. Hidden (not
          unmounted) while Sandbox is active. */}
      <div
        className={
          subtab === "spreadsheet" ? "mx-auto w-full max-w-4xl" : "hidden"
        }
      >
        <ClientsSection expandClientId={clientId ? Number(clientId) : undefined} />
      </div>
    </div>
  );
}
