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
      {/* Spatial canvas — responsive height: compact on mobile (220px gives a
          useful peek without consuming the entire viewport), full 560px on
          sm+ where there's room for the canvas to breathe. */}
      <section
        aria-label="Clients sandbox"
        className="relative overflow-hidden rounded-2xl border border-zinc-200 bg-zinc-50 shadow-sm h-[220px] sm:h-[560px]"
      >
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
