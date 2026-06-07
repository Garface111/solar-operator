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

  return (
    <div className="space-y-8">
      {/* Spatial canvas — full 560px on sm+; on mobile an overlay replaces the
          canvas with a gentle notice (the list view below is the mobile UX). */}
      <section
        aria-label="Clients sandbox"
        className="relative overflow-hidden rounded-2xl border border-zinc-200 bg-zinc-50 shadow-sm h-[220px] sm:h-[560px]"
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
          <SandboxCanvas />
        </ReactFlowProvider>
      </section>

      {/* List view — unchanged. Bulk select, table-style scanning,
          per-row actions. Still the right tool for >50 clients. */}
      <ClientsSection expandClientId={clientId ? Number(clientId) : undefined} />
    </div>
  );
}
