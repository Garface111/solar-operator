import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { SectionTitle } from "../ui/SectionTitle";
import { useToast } from "../ui/Toast";
import { ClientsTable } from "./ClientsTable";
import { AddClientModal } from "./AddClientModal";
import { CaptureListener } from "./CaptureListener";

const AddClientByLoginModal = lazy(() =>
  import("./AddClientByLoginModal").then((m) => ({ default: m.AddClientByLoginModal })),
);
const ImportSpreadsheetModal = lazy(() =>
  import("./ImportSpreadsheetModal").then((m) => ({ default: m.ImportSpreadsheetModal })),
);
const AssignNepoolFromSpreadsheetModal = lazy(() =>
  import("./AssignNepoolFromSpreadsheetModal").then((m) => ({
    default: m.AssignNepoolFromSpreadsheetModal,
  })),
);
const CaptureCeremony = lazy(() =>
  import("./CaptureCeremony").then((m) => ({ default: m.CaptureCeremony })),
);
import {
  type ClientRow,
  listClients,
  bulkDeleteClients,
  undoDelete,
  undoMerge,
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
  const [addingByLogin, setAddingByLogin] = useState(false);
  const [importing, setImporting] = useState(false);
  const [assigningNepool, setAssigningNepool] = useState(false);
  const [missingNepoolCount, setMissingNepoolCount] = useState(0);
  // The NEPOOL banner is loud enough (amber, full-width, top of section) to
  // be discovered naturally. We intentionally do NOT autoscroll to it — fresh
  // captures should let the user dwell on the new client cards in the canvas
  // above (fill in emails, rename, group logins), THEN encounter NEPOOL when
  // they scroll down. The previous autoscroll yanked the page mid-onboarding
  // and felt jarring.
  const nepoolBannerRef = useRef<HTMLDivElement | null>(null);

  // Live polling indicator state.
  const [pollingNewData, setPollingNewData] = useState(false);
  // Refs for polling so closures always read the latest value without re-running effects.
  const clientsRef = useRef<ClientRow[] | null>(null);
  const modalOpenRef = useRef(false);
  const pollingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollingPulseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Keep clientsRef in sync so polling closures always read the latest value.
  useEffect(() => { clientsRef.current = clients; }, [clients]);

  // Track modal open state so polling yields while a modal is up.
  useEffect(() => {
    modalOpenRef.current = adding || addingByLogin || importing || assigningNepool;
  }, [adding, addingByLogin, importing, assigningNepool]);

  // Multi-select state
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

  // Undo bar state
  const [undoPending, setUndoPending] = useState<{
    token: string;
    message: string;
    kind: "delete" | "merge";
  } | null>(null);
  const undoTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const importPollerRef = useRef<PollerHandle | null>(null);

  function scheduleUndo(token: string, message: string, kind: "delete" | "merge" = "delete") {
    if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
    setUndoPending({ token, message, kind });
    undoTimerRef.current = setTimeout(() => setUndoPending(null), 60_000);
  }

  function clearUndo() {
    if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
    setUndoPending(null);
  }

  async function handleUndo(token: string) {
    const kind = undoPending?.kind ?? "delete";
    try {
      if (kind === "merge") {
        await undoMerge(token);
      } else {
        await undoDelete(token);
      }
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
    // Refresh clients when a merge completes anywhere on the page —
    // the merged-from card needs to disappear without a full reload.
    function onMerged() {
      loadClients();
    }
    window.addEventListener("so:client-merged", onMerged);
    return () => {
      if (undoTimerRef.current) clearTimeout(undoTimerRef.current);
      importPollerRef.current?.cancel();
      if (pollingTimerRef.current) clearTimeout(pollingTimerRef.current);
      if (pollingPulseTimerRef.current) clearTimeout(pollingPulseTimerRef.current);
      window.removeEventListener("so:client-merged", onMerged);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Background polling: refetch clients every 15s while the tab is visible.
  // Yields to open modals or active input focus to avoid clobbering optimistic edits.
  // Only updates state when the response actually differs from current state.
  useEffect(() => {
    const POLL_MS = 15_000;
    let cancelled = false;

    async function tick() {
      if (cancelled || document.visibilityState !== "visible") return;

      // Yield if a modal is open or user is typing in an editable field.
      const isEditing =
        modalOpenRef.current ||
        document.activeElement instanceof HTMLInputElement ||
        document.activeElement instanceof HTMLTextAreaElement;

      if (!isEditing) {
        try {
          const fresh = await listClients();
          if (!cancelled && clientsRef.current !== null) {
            if (JSON.stringify(fresh) !== JSON.stringify(clientsRef.current)) {
              setClients(fresh);
              setPollingNewData(true);
              if (pollingPulseTimerRef.current) clearTimeout(pollingPulseTimerRef.current);
              pollingPulseTimerRef.current = setTimeout(
                () => setPollingNewData(false),
                1_000,
              );
            }
          }
        } catch { /* non-fatal — leave stale rather than wiping */ }
      }

      if (!cancelled && document.visibilityState === "visible") {
        pollingTimerRef.current = setTimeout(tick, POLL_MS);
      }
    }

    function onVisibility() {
      if (cancelled) return;
      if (document.visibilityState === "visible") {
        // Return from background — refetch immediately then resume interval.
        if (pollingTimerRef.current) clearTimeout(pollingTimerRef.current);
        void tick();
      } else {
        // Tab hidden — pause the interval (tick won't reschedule while hidden).
        if (pollingTimerRef.current) clearTimeout(pollingTimerRef.current);
        pollingTimerRef.current = null;
      }
    }

    document.addEventListener("visibilitychange", onVisibility);
    // Start after initial load has had time to settle.
    pollingTimerRef.current = setTimeout(tick, POLL_MS);

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      // Timer refs cleaned up in the unmount effect above.
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function loadNepoolStats() {
    getNepoolStats()
      .then((s) => setMissingNepoolCount(s.arrays_missing_nepool))
      .catch(() => { /* non-critical, ignore */ });
  }

  function loadClients(): Promise<ClientRow[]> {
    return listClients()
      .then((rows) => {
        setClients(rows);
        return rows;
      })
      .catch((err) => {
        toast.error(err instanceof Error ? err.message : "Couldn't load clients");
        setClients([]);
        return [] as ClientRow[];
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

    // Live-refresh whenever the sandbox above mutates its state (reparent,
    // merge, detach, delete, etc.). Coalesce rapid bursts with a short debounce
    // so dragging across multiple cards doesn't N+1 the backend.
    let debounce: ReturnType<typeof setTimeout> | null = null;
    const onSandboxMutated = () => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        if (cancelled) return;
        listClients()
          .then((rows) => { if (!cancelled) setClients(rows); })
          .catch(() => { /* leave stale rather than wiping */ });
        getNepoolStats()
          .then((s) => { if (!cancelled) setMissingNepoolCount(s.arrays_missing_nepool); })
          .catch(() => { /* non-critical */ });
      }, 150);
    };
    window.addEventListener('so:sandbox:mutated', onSandboxMutated);
    // Bruce Jun 6: NEPOOL banner didn't clear after inline-edit assignment.
    // ArrayList + AssignNepoolFromSpreadsheetModal both dispatch
    // 'so:arrays-changed' on save/commit; that path was previously ignored
    // here, so the banner kept its stale count until a full page reload.
    // Reuse the same debounced refresh as sandbox mutations.
    window.addEventListener('so:arrays-changed', onSandboxMutated);
    getNepoolStats()
      .then((s) => { if (!cancelled) setMissingNepoolCount(s.arrays_missing_nepool); })
      .catch(() => { /* non-critical */ });
    return () => {
      cancelled = true;
      if (debounce) clearTimeout(debounce);
      window.removeEventListener('so:sandbox:mutated', onSandboxMutated);
      window.removeEventListener('so:arrays-changed', onSandboxMutated);
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
      {/* Global capture listener — turns SO_CAPTURE_LANDED postMessages
          into toasts on whichever tab the operator is currently on.
          Decoupled from the Add Client modal so success notifications
          fire even after the modal has closed. */}
      <CaptureListener onCaptureLanded={loadClients} />

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
        <div
          ref={nepoolBannerRef}
          data-walkthrough="nepool-banner"
          className="mb-4 rounded-2xl border-2 border-amber-300 bg-gradient-to-br from-amber-50 via-amber-50/80 to-white px-5 py-4 shadow-sm"
        >
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex-1 min-w-0">
              <p className="text-[11px] font-medium uppercase tracking-wider text-amber-700">
                Step 2 · Add NEPOOL-GIS IDs
              </p>
              <p className="mt-1 text-base font-semibold text-zinc-900">
                {missingNepoolCount} array{missingNepoolCount === 1 ? "" : "s"} need{missingNepoolCount === 1 ? "s" : ""} a NEPOOL ID to ship
              </p>
              <p className="mt-1.5 text-sm leading-relaxed text-zinc-700">
                The NEPOOL-GIS ID is the 4–6 digit code (sometimes labeled <em>NEPOOL</em>, <em>GIS</em>, or <em>Asset ID</em>) that
                identifies each array in Vermont&apos;s REC market. We need it to attribute the right RECs
                to the right client.
              </p>
              <ul className="mt-3 space-y-1 text-xs text-zinc-600">
                <li className="flex items-start gap-1.5">
                  <span className="text-amber-600">▸</span>
                  <span><strong className="text-zinc-800">Have a spreadsheet?</strong> Drop it below — we&apos;ll match NEPOOL IDs to your arrays automatically.</span>
                </li>
                <li className="flex items-start gap-1.5">
                  <span className="text-amber-600">▸</span>
                  <span><strong className="text-zinc-800">Don&apos;t have one?</strong> Click any array name in the canvas above to edit its NEPOOL ID inline.</span>
                </li>
              </ul>
            </div>
            <button
              type="button"
              onClick={() => setAssigningNepool(true)}
              className="shrink-0 rounded-xl bg-amber-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-amber-700 active:bg-amber-800"
            >
              Import NEPOOL IDs from spreadsheet →
            </button>
          </div>
        </div>
      )}

      {/* Sublime moment — capture ceremony. Listens for SO_CAPTURE_LANDED
          broadcasts from the extension; renders cascading client+array
          chips and prompts "log into another portal" so the operator
          rides the dopamine loop on every new login. freshVisit=true
          surfaces it pre-emptively for post-onboarding arrivals. */}
      <Suspense fallback={null}>
        <CaptureCeremony
          freshVisit={new URLSearchParams(window.location.search).get("fresh") === "1"}
          onCaptureLanded={loadClients}
        />
      </Suspense>

      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <SectionTitle title="Clients" count={clients?.length} />
          {/* Live polling indicator: subtle at 50% opacity, pulses green for 1s on new data. */}
          <span
            aria-hidden
            title="Live — auto-refreshes every 15s"
            className={[
              "h-2 w-2 rounded-full bg-green-500 transition-opacity duration-700",
              pollingNewData ? "animate-pulse opacity-100" : "opacity-50",
            ].join(" ")}
          />
        </div>
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
            onClick={() => setAddingByLogin(true)}
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
              <Button onClick={() => setAddingByLogin(true)}>+ Add your first client</Button>
            </div>
          </div>
        </Card>
      ) : (
        <ClientsTable
          clients={clients}
          operatorEmail={operatorEmail}
          accountReportFrequency={account?.report_frequency ?? null}
          expandClientId={expandClientId}
          selectMode={selectMode}
          selectedIds={selectedIds}
          onToggleSelect={toggleSelect}
          onChange={replaceClient}
          onDeleted={(id, token, msg) => {
            removeClientLocal(id);
            scheduleUndo(token, msg, "delete");
          }}
          onUndo={scheduleUndo}
          onOpenAddByLogin={() => setAddingByLogin(true)}
          allClients={clients}
          onMerged={(dst, _srcId, undoToken) => {
            scheduleUndo(undoToken, `Merged into "${dst.name}"`, "merge");
          }}
        />
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

      {addingByLogin && (
        <Suspense fallback={null}>
          <AddClientByLoginModal
            open={addingByLogin}
            onClose={() => setAddingByLogin(false)}
            onCaptured={loadClients}
            onSwitchToManual={() => {
              setAddingByLogin(false);
              setAdding(true);
            }}
          />
        </Suspense>
      )}

      {importing && (
        <Suspense fallback={null}>
          <ImportSpreadsheetModal
            open={importing}
            onClose={() => setImporting(false)}
            onImported={handleImported}
          />
        </Suspense>
      )}

      {assigningNepool && (
        <Suspense fallback={null}>
          <AssignNepoolFromSpreadsheetModal
            open={assigningNepool}
            onClose={() => setAssigningNepool(false)}
            onAssigned={() => {
              loadClients();
              loadNepoolStats();
            }}
          />
        </Suspense>
      )}
    </section>
  );
}
