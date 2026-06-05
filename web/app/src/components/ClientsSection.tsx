import { useEffect, useRef, useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/Toast";
import { ClientCard } from "./ClientCard";
import { AddClientModal } from "./AddClientModal";
import { ImportSpreadsheetModal } from "./ImportSpreadsheetModal";
import { AssignNepoolFromSpreadsheetModal } from "./AssignNepoolFromSpreadsheetModal";
import { CaptureCeremony } from "./CaptureCeremony";
import {
  type ClientRow,
  listClients,
  bulkDeleteClients,
  undoDelete,
  getNepoolStats,
} from "../lib/api";
import { type PollerHandle, pollUntilChanged } from "../lib/poller";
import { useDashboardContext } from "../screens/DashboardLayout";

interface Props {
  /** Client id to auto-expand on load (from a /clients/:id deep link). */
  expandClientId?: number;
}

export function ClientsSection({ expandClientId }: Props) {
  const toast = useToast();
  const { account } = useDashboardContext();
  const operatorEmail = account?.email ?? null;
  const [clients, setClients] = useState<ClientRow[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [assigningNepool, setAssigningNepool] = useState(false);
  const [missingNepoolCount, setMissingNepoolCount] = useState(0);

  // Multi-select state
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

  // Undo bar state
  const [undoPending, setUndoPending] = useState<{ token: string; message: string } | null>(null);
  const undoTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const importPollerRef = useRef<PollerHandle | null>(null);

  function scheduleUndo(token: string, message: string) {
    if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
    setUndoPending({ token, message });
    undoTimerRef.current = setTimeout(() => setUndoPending(null), 60_000);
  }

  function clearUndo() {
    if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
    setUndoPending(null);
  }

  async function handleUndo(token: string) {
    try {
      await undoDelete(token);
      clearUndo();
      loadClients();
      toast.success("Restored");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Undo failed");
      clearUndo();
    }
  }

  // Clean up timer on unmount
  useEffect(() => {
    return () => {
      if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
      importPollerRef.current?.cancel();
    };
  }, []);

  function loadNepoolStats() {
    getNepoolStats()
      .then((s) => setMissingNepoolCount(s.arrays_missing_nepool))
      .catch(() => { /* non-critical, ignore */ });
  }

  function loadClients() {
    listClients()
      .then(setClients)
      .catch((err) => {
        toast.error(err instanceof Error ? err.message : "Couldn't load clients");
        setClients([]);
      });
  }

  function handleImported() {
    loadClients();
    importPollerRef.current?.cancel();
    const [p, handle] = pollUntilChanged(
      listClients,
      (prev, next) => {
        if (next.length !== prev.length) return true;
        const prevTotal = prev.reduce((s, c) => s + c.array_count, 0);
        const nextTotal = next.reduce((s, c) => s + c.array_count, 0);
        return nextTotal !== prevTotal;
      },
    );
    importPollerRef.current = handle;
    p.then((newClients) => {
      if (newClients) setClients(newClients);
    });
  }

  useEffect(() => {
    let cancelled = false;
    listClients()
      .then((rows) => {
        if (!cancelled) setClients(rows);
      })
      .catch((err) => {
        if (!cancelled) {
          toast.error(err instanceof Error ? err.message : "Couldn't load clients");
          setClients([]);
        }
      });
    getNepoolStats()
      .then((s) => { if (!cancelled) setMissingNepoolCount(s.arrays_missing_nepool); })
      .catch(() => { /* non-critical */ });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function replaceClient(updated: ClientRow) {
    setClients((cs) => (cs ? cs.map((c) => (c.id === updated.id ? updated : c)) : cs));
  }

  function removeClientLocal(id: number) {
    setClients((cs) => (cs ? cs.filter((c) => c.id !== id) : cs));
    setSelectedIds((s) => { const n = new Set(s); n.delete(id); return n; });
  }

  function addClientLocal(c: ClientRow) {
    setClients((cs) => (cs ? [...cs, c].sort((a, b) => a.name.localeCompare(b.name)) : [c]));
  }

  function toggleSelect(id: number) {
    setSelectedIds((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  }

  function exitSelectMode() {
    setSelectMode(false);
    setSelectedIds(new Set());
  }

  async function handleBulkDelete() {
    if (!selectedIds.size || bulkDeleting) return;
    setBulkDeleting(true);
    try {
      const ids = Array.from(selectedIds);
      const res = await bulkDeleteClients(ids);
      ids.forEach(removeClientLocal);
      exitSelectMode();
      setBulkConfirm(false);
      const n = res.soft_deleted;
      scheduleUndo(res.undo_token, `Deleted ${n} client${n === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete clients");
    } finally {
      setBulkDeleting(false);
    }
  }

  const bouncedClients =
    clients?.filter(
      (c) =>
        c.active &&
        c.last_bounced_at &&
        (!c.last_delivered_at ||
          new Date(c.last_bounced_at) > new Date(c.last_delivered_at)),
    ) ?? [];

  return (
    <section className="relative">
      {/* Undo banner — fixed at top of viewport */}
      {undoPending && (
        <div className="fixed inset-x-0 top-0 z-50 flex items-center justify-between gap-4 border-b border-amber-300 bg-amber-50 px-6 py-3 text-sm shadow-md">
          <span className="text-amber-900">{undoPending.message}</span>
          <div className="flex shrink-0 items-center gap-4">
            <button
              type="button"
              onClick={() => handleUndo(undoPending.token)}
              className="font-semibold text-amber-900 hover:text-amber-700 focus:outline-none"
            >
              Undo
            </button>
            <button
              type="button"
              onClick={clearUndo}
              aria-label="Dismiss"
              className="text-amber-600 hover:text-amber-500 focus:outline-none"
            >
              ✕
            </button>
          </div>
        </div>
      )}

      {bouncedClients.length > 0 && (
        <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-sm font-semibold text-red-900">
            {bouncedClients.length === 1
              ? "1 client has a bounced delivery email"
              : `${bouncedClients.length} clients have bounced delivery emails`}
          </p>
          <p className="mt-1 text-xs text-red-800">
            {bouncedClients.map((c) => c.name).join(", ")} — update their
            contact email so reports reach them.
          </p>
        </div>
      )}

      {missingNepoolCount > 0 && (
        <div className="mb-4 flex items-center justify-between gap-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <div>
            <p className="text-sm font-semibold text-amber-900">
              {missingNepoolCount} array{missingNepoolCount === 1 ? " is" : "s are"} missing NEPOOL IDs.
            </p>
            <p className="mt-0.5 text-xs text-amber-800">
              Reports for those clients won&apos;t ship without them.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setAssigningNepool(true)}
            className="shrink-0 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-sm font-medium text-amber-900 hover:bg-amber-100 focus:outline-none"
          >
            Find them in a spreadsheet
          </button>
        </div>
      )}

      {/* Sublime moment — capture ceremony. Listens for SO_CAPTURE_LANDED
          broadcasts from the extension; renders cascading client+array
          chips and prompts "log into another portal" so the operator
          rides the dopamine loop on every new login. freshVisit=true
          surfaces it pre-emptively for post-onboarding arrivals. */}
      <CaptureCeremony
        freshVisit={new URLSearchParams(window.location.search).get("fresh") === "1"}
        onCaptureLanded={loadClients}
      />

      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
          Clients
          {clients && (
            <span className="ml-2 text-sm font-normal text-zinc-400">
              {clients.length}
            </span>
          )}
        </h2>
        <div className="flex items-center gap-2">
          {clients && clients.length > 0 && (
            <button
              type="button"
              onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
              className={[
                "rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors focus:outline-none",
                selectMode
                  ? "border-primary-300 bg-primary-50 text-primary-700"
                  : "border-zinc-300 bg-white text-zinc-600 hover:border-zinc-400",
              ].join(" ")}
            >
              {selectMode ? "Cancel select" : "Select"}
            </button>
          )}
          <Button
            variant="secondary"
            onClick={() => setImporting(true)}
            className="px-4 py-2"
            data-tour-step="6-import"
          >
            Import spreadsheet
          </Button>
          <Button
            onClick={() => setAdding(true)}
            className="px-4 py-2"
            data-tour-step="6-add"
          >
            + Add client
          </Button>
        </div>
      </div>

      {clients === null ? (
        <Card>
          <div className="flex items-center gap-2 text-sm text-zinc-400">
            <Spinner className="h-4 w-4" />
            Loading clients…
          </div>
        </Card>
      ) : clients.length === 0 ? (
        <Card>
          <div className="space-y-3">
            <h3 className="text-base font-semibold text-zinc-900">
              Add your first client to auto-detect their arrays
            </h3>
            <p className="text-sm text-zinc-600">
              For each client, add their name and the utility login they use to
              sign in. Then open their utility portal once signed in with that
              client&apos;s login — the extension captures their bills and
              creates the arrays for you. You only do this once per client.
            </p>
            <ol className="ml-5 list-decimal space-y-1 text-sm text-zinc-700">
              <li>Click <b>+ Add client</b> and enter their utility login.</li>
              <li>Open <a href="https://greenmountainpower.com" target="_blank" rel="noopener noreferrer" className="text-primary-600 underline-offset-2 hover:underline">greenmountainpower.com</a> signed in as that client.</li>
              <li>Their arrays show up here automatically.</li>
            </ol>
            <div className="pt-1">
              <Button onClick={() => setAdding(true)}>+ Add your first client</Button>
            </div>
          </div>
        </Card>
      ) : (
        <div className="space-y-3">
          {clients.map((c) => (
            <ClientCard
              key={c.id}
              client={c}
              operatorEmail={operatorEmail}
              defaultExpanded={c.id === expandClientId}
              onChange={replaceClient}
              selectable={selectMode}
              selected={selectedIds.has(c.id)}
              onSelect={toggleSelect}
              onDeleted={(token, msg) => {
                removeClientLocal(c.id);
                scheduleUndo(token, msg);
              }}
              onUndo={scheduleUndo}
            />
          ))}
        </div>
      )}

      {/* Sticky bulk-action bar */}
      {selectMode && selectedIds.size > 0 && (
        <div className="sticky bottom-4 mt-4 flex items-center justify-between rounded-xl border border-zinc-200 bg-white px-5 py-3 shadow-lg">
          <span className="text-sm text-zinc-600">
            {selectedIds.size} client{selectedIds.size === 1 ? "" : "s"} selected
          </span>
          <Button variant="danger" onClick={() => setBulkConfirm(true)}>
            Delete {selectedIds.size} client{selectedIds.size === 1 ? "" : "s"}
          </Button>
        </div>
      )}

      {/* Bulk delete confirmation */}
      <Modal
        open={bulkConfirm}
        onClose={() => !bulkDeleting && setBulkConfirm(false)}
        title={`Delete ${selectedIds.size} client${selectedIds.size === 1 ? "" : "s"}?`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setBulkConfirm(false)} disabled={bulkDeleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleBulkDelete} disabled={bulkDeleting}>
              {bulkDeleting ? <><Spinner /> Deleting…</> : `Delete ${selectedIds.size} client${selectedIds.size === 1 ? "" : "s"}`}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          This will also delete all arrays and utility accounts under the selected{" "}
          {selectedIds.size === 1 ? "client" : "clients"}.{" "}
          <span className="font-medium text-zinc-800">You'll have 5 minutes to undo.</span>
        </p>
        {clients && selectedIds.size > 0 && (
          <ul className="mt-2 space-y-0.5 text-sm text-zinc-700">
            {clients
              .filter((c) => selectedIds.has(c.id))
              .map((c) => (
                <li key={c.id} className="truncate">• {c.name}</li>
              ))}
          </ul>
        )}
      </Modal>

      <AddClientModal
        open={adding}
        onClose={() => setAdding(false)}
        onCreated={addClientLocal}
      />

      <ImportSpreadsheetModal
        open={importing}
        onClose={() => setImporting(false)}
        onImported={handleImported}
      />

      <AssignNepoolFromSpreadsheetModal
        open={assigningNepool}
        onClose={() => setAssigningNepool(false)}
        onAssigned={() => {
          loadClients();
          loadNepoolStats();
        }}
      />
    </section>
  );
}
