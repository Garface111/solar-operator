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
      {/* Spatial canvas — constrained height so the list below stays
          discoverable. 560px lets ~6-9 client cards breathe at default
          zoom; operators can pan/zoom freely inside. */}
      <section
        aria-label="Clients sandbox"
        className="relative overflow-hidden rounded-2xl border border-zinc-200 bg-zinc-50 shadow-sm"
        style={{ height: 560 }}
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
