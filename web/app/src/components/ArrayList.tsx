import { useEffect, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import { ArrayMergeSuggestionBanner } from "./ArrayMergeSuggestionBanner";
import { InlineNepoolField } from "./InlineNepoolField";
import { FuelPicker, FuelBadge } from "./FuelControls";
import { DEFAULT_FUEL, type FuelType } from "../lib/fuel";
import {
  type ArrayRow as ArrayRowT,
  type Provider,
  type SolarEdgeSite,
  listArrays,
  createArray,
  updateArray,
  deleteArray,
  bulkDeleteArrays,
  addUtilityAccount,
  removeUtilityAccount,
  listProviders,
  uploadDailyCsv,
  setupSolarEdge,
  previewSolarEdge,
  disconnectSolarEdge,
} from "../lib/api";

interface Props {
  clientId: number;
  /** Increment to trigger a re-fetch of arrays from the server. */
  refreshSignal?: number;
  onCountChange?: (count: number) => void;
  onUndo?: (token: string, message: string) => void;
  /**
   * When set (ms), array rows fade in with a staggered cascade starting
   * at this offset — used so the welcome reveal flows through arrays
   * instead of stopping at the card header.
   */
  revealStartDelayMs?: number;
}

export function ArrayList({ clientId, refreshSignal, onCountChange, onUndo, revealStartDelayMs }: Props) {
  const toast = useToast();
  const [arrays, setArrays] = useState<ArrayRowT[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  // Anchor for shift-click range selection — the last row toggled without shift.
  const [lastClickedId, setLastClickedId] = useState<number | null>(null);
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);
  // "Show only missing NEPOOL" — lets an operator with 40+ arrays jump straight
  // to the handful that still need an ID instead of scrolling the whole list.
  const [onlyMissing, setOnlyMissing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    function load() {
      listArrays(clientId)
        .then((rows) => {
          if (!cancelled) setArrays(rows);
        })
        .catch((err) => {
          if (!cancelled) {
            toast.error(err instanceof Error ? err.message : "Couldn't load arrays");
            setArrays([]);
          }
        });
    }
    load();
    // Listen for the same broadcast we emit on local mutations — covers the
    // NEPOOL-import / autopop / master-import cases where the mutation
    // happened in a sibling component (modal, ClientsSection) and the
    // parent has no way to bump refreshSignal on every mounted ArrayList.
    function onChanged() {
      load();
    }
    window.addEventListener("so:arrays-changed", onChanged);
    return () => {
      cancelled = true;
      window.removeEventListener("so:arrays-changed", onChanged);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId, refreshSignal]);

  // Select-mode keyboard shortcuts: "a"/Ctrl+Cmd+a → select all, Esc → cancel.
  // Skipped while typing in a field so it doesn't hijack editing.
  useEffect(() => {
    if (!selectMode) return;
    function onKey(e: KeyboardEvent) {
      const el = document.activeElement as HTMLElement | null;
      const tag = el?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || el?.isContentEditable) return;
      if (e.key === "Escape") {
        if (bulkConfirm) return; // let the open modal handle its own Escape
        exitSelectMode();
      } else if (e.key === "a" || e.key === "A") {
        e.preventDefault();
        setSelectedIds(new Set((arrays ?? []).map((a) => a.id)));
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectMode, arrays, bulkConfirm]);

  function replaceArray(updated: ArrayRowT) {
    setArrays((rows) =>
      rows ? rows.map((a) => (a.id === updated.id ? updated : a)) : rows,
    );
  }

  function addArrayLocal(a: ArrayRowT) {
    setArrays((rows) => {
      const next = rows ? [...rows, a] : [a];
      onCountChange?.(next.length);
      return next;
    });
    window.dispatchEvent(new CustomEvent("so:arrays-changed"));
  }

  function removeArrayLocal(id: number) {
    setArrays((rows) => {
      const next = rows ? rows.filter((a) => a.id !== id) : rows;
      if (next) onCountChange?.(next.length);
      return next;
    });
    setSelectedIds((s) => { const n = new Set(s); n.delete(id); return n; });
    window.dispatchEvent(new CustomEvent("so:arrays-changed"));
  }

  function toggleSelect(id: number, event?: React.MouseEvent) {
    const shift = !!event?.shiftKey;
    setSelectedIds((s) => {
      const n = new Set(s);
      const rows = arrays ?? [];
      // Shift-click: select/deselect the whole range between the anchor row
      // and the clicked row. The range adopts whatever state the clicked row
      // is heading toward (select if it was unselected, else deselect).
      if (shift && lastClickedId !== null) {
        const startIdx = rows.findIndex((a) => a.id === lastClickedId);
        const endIdx = rows.findIndex((a) => a.id === id);
        if (startIdx !== -1 && endIdx !== -1) {
          const willSelect = !n.has(id);
          const [lo, hi] = startIdx <= endIdx ? [startIdx, endIdx] : [endIdx, startIdx];
          for (let i = lo; i <= hi; i++) {
            if (willSelect) n.add(rows[i].id); else n.delete(rows[i].id);
          }
          return n;
        }
      }
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
    setLastClickedId(id);
  }

  // "Select all" targets the rendered (visible) rows only — defensive against a
  // future filter/sort, where hidden rows shouldn't be swept in.
  const visibleArrays = arrays ?? [];
  const allVisibleSelected =
    visibleArrays.length > 0 && visibleArrays.every((a) => selectedIds.has(a.id));

  function selectAllVisible() {
    setSelectedIds(new Set(visibleArrays.map((a) => a.id)));
  }

  function exitSelectMode() {
    setSelectMode(false);
    setSelectedIds(new Set());
    setLastClickedId(null);
  }

  async function handleBulkDelete() {
    if (!selectedIds.size || bulkDeleting) return;
    setBulkDeleting(true);
    try {
      const ids = Array.from(selectedIds);
      const res = await bulkDeleteArrays(ids);
      ids.forEach(removeArrayLocal);
      exitSelectMode();
      setBulkConfirm(false);
      const n = res.soft_deleted;
      onUndo?.(res.undo_token, `Deleted ${n} array${n === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete arrays");
    } finally {
      setBulkDeleting(false);
    }
  }

  if (arrays === null) {
    return (
      <div className="flex items-center gap-2 px-1 py-4 text-sm text-zinc-400">
        <Spinner className="h-4 w-4" />
        Loading arrays…
      </div>
    );
  }

  const missingCount = arrays.filter((a) => !a.nepool_gis_id).length;
  const visible = onlyMissing ? arrays.filter((a) => !a.nepool_gis_id) : arrays;

  return (
    <div className="space-y-2">
      {/* Action bar: Select-mode toggle + bulk-delete + "missing NEPOOL" filter. */}
      {arrays.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => (selectMode ? exitSelectMode() : setSelectMode(true))}
              className={[
                "rounded border px-2.5 py-1 text-xs font-medium transition-colors focus:outline-none",
                selectMode
                  ? "border-primary-300 bg-primary-50 text-primary-700"
                  : "border-zinc-200 text-zinc-500 hover:border-zinc-300",
              ].join(" ")}
            >
              {selectMode ? "Cancel" : "Select"}
            </button>
            {selectMode && visibleArrays.length > 0 && (
              <button
                type="button"
                onClick={() =>
                  allVisibleSelected ? setSelectedIds(new Set()) : selectAllVisible()
                }
                className="rounded border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-600 transition-colors hover:border-zinc-300 hover:bg-zinc-50 focus:outline-none"
              >
                {allVisibleSelected ? "Select none" : `Select all (${visibleArrays.length})`}
              </button>
            )}
            {selectMode && selectedIds.size > 0 && (
              <button
                type="button"
                onClick={() => setBulkConfirm(true)}
                className="rounded border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 focus:outline-none"
              >
                Delete {selectedIds.size} array{selectedIds.size === 1 ? "" : "s"}
              </button>
            )}
          </div>
          {missingCount > 0 && (
            <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-zinc-500">
              <input
                type="checkbox"
                checked={onlyMissing}
                onChange={(e) => setOnlyMissing(e.target.checked)}
                className="h-3.5 w-3.5 accent-amber-500"
              />
              <span className="inline-flex items-center gap-1.5">
                <span aria-hidden className="h-2 w-2 shrink-0 rounded-full bg-amber-500" />
                Show only missing NEPOOL ({missingCount})
              </span>
            </label>
          )}
        </div>
      )}

      {arrays.length === 0 && !adding && (
        <p className="rounded-xl border border-dashed border-zinc-200 px-4 py-6 text-center text-sm text-zinc-400">
          No arrays yet. Arrays appear here once utility auto-populate runs, or add
          one manually.
        </p>
      )}

      {/* Compact table: rows butt against each other separated by a 1px rule. */}
      {visible.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-cream-border">
          {visible.map((a, idx) => {
            const cascading = revealStartDelayMs !== undefined;
            const cascadeStyle = cascading
              ? ({ animationDelay: `${revealStartDelayMs + idx * 110}ms` } as React.CSSProperties)
              : undefined;
            return (
              <div
                key={a.id}
                style={cascadeStyle}
                className={[
                  "border-b border-cream-border last:border-b-0",
                  cascading ? "so-reveal-card" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <ArrayRow
                  clientId={clientId}
                  array={a}
                  onChange={replaceArray}
                  onDelete={(id, token, name) => {
                    removeArrayLocal(id);
                    onUndo?.(token, `Deleted ${name}`);
                  }}
                  selectable={selectMode}
                  selected={selectedIds.has(a.id)}
                  onSelect={toggleSelect}
                />
              </div>
            );
          })}
        </div>
      )}

      {onlyMissing && visible.length === 0 && (
        <p className="px-1 py-2 text-sm text-zinc-400">
          All arrays have a NEPOOL ID. 🎉
        </p>
      )}

      {adding ? (
        <AddArrayRow
          clientId={clientId}
          onCancel={() => setAdding(false)}
          onCreated={(a) => {
            // Keep the form open for bulk entry — AddArrayRow clears its own
            // fields and refocuses. "Done" (onCancel) closes it.
            addArrayLocal(a);
          }}
        />
      ) : (
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="rounded text-sm font-medium text-primary-600 transition-colors hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
        >
          + Add array
        </button>
      )}

      {/* Floating bottom action bar — mirrors the top bar so you don't have to
          scroll back up after selecting the last row. Sticky within the list on
          desktop; fixed to the viewport bottom on mobile. */}
      {selectMode && selectedIds.size > 0 && (
        <div className="sticky bottom-2 z-30 mt-3 flex items-center justify-between rounded-lg border border-red-200 bg-white/95 px-3 py-2 shadow-sm backdrop-blur-sm max-sm:fixed max-sm:inset-x-3 max-sm:bottom-3">
          <span className="text-xs font-medium text-zinc-700">
            {selectedIds.size} selected
          </span>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={exitSelectMode}
              className="rounded border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-600 transition-colors hover:border-zinc-300 focus:outline-none"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => setBulkConfirm(true)}
              className="rounded border border-red-200 bg-red-50 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-100 focus:outline-none"
            >
              Delete {selectedIds.size} array{selectedIds.size === 1 ? "" : "s"}
            </button>
          </div>
        </div>
      )}

      <Modal
        open={bulkConfirm}
        onClose={() => !bulkDeleting && setBulkConfirm(false)}
        title={`Delete ${selectedIds.size} array${selectedIds.size === 1 ? "" : "s"}?`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setBulkConfirm(false)} disabled={bulkDeleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleBulkDelete} disabled={bulkDeleting}>
              {bulkDeleting ? (
                <><Spinner /> Deleting…</>
              ) : (
                `Delete ${selectedIds.size} array${selectedIds.size === 1 ? "" : "s"}`
              )}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          {selectedIds.size === 1 ? "This array" : `These ${selectedIds.size} arrays`} will be
          removed.{" "}
          <span className="font-medium text-zinc-800">You'll have 5 minutes to undo.</span>
        </p>
        {arrays && selectedIds.size > 0 && (
          selectedIds.size > 20 ? (
            // Compact 3-column grid so 30+ names stay visible without scrolling.
            <ul className="mt-2 grid grid-cols-3 gap-x-3 gap-y-1 text-xs text-zinc-700">
              {arrays.filter((a) => selectedIds.has(a.id)).map((a) => (
                <li key={a.id} className="truncate" title={a.name}>• {a.name}</li>
              ))}
            </ul>
          ) : (
            <ul className="mt-2 space-y-0.5 text-sm text-zinc-700">
              {arrays.filter((a) => selectedIds.has(a.id)).map((a) => (
                <li key={a.id} className="truncate">• {a.name}</li>
              ))}
            </ul>
          )
        )}
      </Modal>
    </div>
  );
}

// ─── one array row ─────────────────────────────────────────────────────────

function ArrayRow({
  clientId,
  array,
  onChange,
  onDelete,
  selectable,
  selected,
  onSelect,
}: {
  clientId: number;
  array: ArrayRowT;
  onChange: (a: ArrayRowT) => void;
  onDelete: (id: number, token: string, name: string) => void;
  selectable?: boolean;
  selected?: boolean;
  onSelect?: (id: number, event?: React.MouseEvent) => void;
}) {
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadedDays, setUploadedDays] = useState<number | null>(null);
  const csvInputRef = useRef<HTMLInputElement | null>(null);

  // SolarEdge modal state
  const [seModalOpen, setSeModalOpen] = useState(false);
  const [seApiKey, setSeApiKey] = useState("");
  const [seSaving, setSeSaving] = useState(false);
  const [seSites, setSeSites] = useState<SolarEdgeSite[] | null>(null);
  const [seSelectedSiteId, setSeSelectedSiteId] = useState<number | "">("");
  const [sePreview, setSePreview] = useState<{ days_pulled: number; sample: { day: string; kwh: number }[] } | null>(null);
  const [seConnected, setSeConnected] = useState(array.solaredge_connected);
  const [seDisconnecting, setSeDisconnecting] = useState(false);

  async function save(patch: Partial<ArrayRowT>) {
    const updated = await updateArray(clientId, array.id, patch as any);
    onChange(updated);
    // Bruce Jun 6: inline NEPOOL-ID save didn't clear the "Add NEPOOL ID" banner
    // until refresh. ClientsSection listens for so:arrays-changed to refetch
    // /nepool-stats; bulk-delete + merge + spreadsheet-assign already fire it.
    window.dispatchEvent(new CustomEvent("so:arrays-changed"));
  }

  async function handleCsvUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || uploading) return;
    // Reset so re-selecting the same file fires onChange again
    e.target.value = "";
    setUploading(true);
    try {
      const result = await uploadDailyCsv(array.id, file);
      const total = result.rows_inserted + result.rows_updated;
      setUploadedDays(total);
      const range = result.date_range
        ? ` (${result.date_range.start} → ${result.date_range.end})`
        : "";
      const fmtNote =
        result.detected_format === "no-header-fallback"
          ? " · no header found, read column 1 as date and column 2 as kWh"
          : "";
      toast.success(`Uploaded ${total} days${range}${fmtNote}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete() {
    if (deleting) return;
    setDeleting(true);
    try {
      const res = await deleteArray(clientId, array.id);
      onDelete(array.id, res.undo_token, array.name);
      setConfirmDelete(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete array");
      setDeleting(false);
      setConfirmDelete(false);
    }
  }

  function openSeModal() {
    setSeApiKey("");
    setSeSites(null);
    setSeSelectedSiteId("");
    setSePreview(null);
    setSeModalOpen(true);
  }

  async function handleSeConnect() {
    if (seSaving) return;
    const key = seApiKey.trim();
    if (!key) { toast.error("Paste your SolarEdge API key first"); return; }
    setSeSaving(true);
    try {
      const siteId = seSelectedSiteId !== "" ? (seSelectedSiteId as number) : undefined;
      const result = await setupSolarEdge(clientId, array.id, key, siteId);
      if (result.needs_site_selection) {
        if (result.sites && result.sites.length > 0) {
          setSeSites(result.sites);
          setSeSelectedSiteId(result.sites[0].site_id);
        } else {
          setSeSites([]);
        }
        return;
      }
      // Connected — fire preview
      setSeConnected(true);
      toast.success(`Connected to SolarEdge site "${result.site_name}" (${result.peak_kw} kW)`);
      try {
        const preview = await previewSolarEdge(clientId, array.id);
        setSePreview(preview);
      } catch {
        // Preview failure is non-fatal
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "SolarEdge connection failed");
    } finally {
      setSeSaving(false);
    }
  }

  async function handleSeDisconnect() {
    if (seDisconnecting) return;
    setSeDisconnecting(true);
    try {
      await disconnectSolarEdge(clientId, array.id);
      setSeConnected(false);
      setSeModalOpen(false);
      toast.success("SolarEdge disconnected. Historical data is preserved.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't disconnect SolarEdge");
    } finally {
      setSeDisconnecting(false);
    }
  }

  return (
    <div
      data-nepool-array-id={array.id}
      data-nepool-client-id={clientId}
      data-nepool-empty={!array.nepool_gis_id ? "true" : undefined}
      className={[
        "transition-colors",
        array.excluded ? "opacity-60" : "",
        selected ? "bg-primary-50 ring-1 ring-inset ring-primary-300" : "hover:bg-cream/60",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {/* Compact collapsed row: checkbox (select-mode) · name · NEPOOL · chevron */}
      <div className="flex h-10 items-center gap-2 px-2 sm:px-3">
        {selectable && (
          <input
            type="checkbox"
            checked={!!selected}
            onChange={() => onSelect?.(array.id)}
            aria-label={`Select ${array.name}`}
            className="h-4 w-4 shrink-0 accent-primary-500"
          />
        )}
        <div className="min-w-0 flex-1 overflow-hidden">
          <EditableField
            value={array.name}
            label="array name"
            onSave={(v) => save({ name: v })}
            emptyText="Unnamed array"
            className="max-w-full font-medium"
          />
        </div>
        {/* Fuel badge — renders only for non-solar arrays so a mixed-fuel
            operator can tell wind/hydro/etc. apart at a glance. Solar shows
            nothing, keeping the solar-only list visually unchanged. */}
        <FuelBadge fuel={array.fuel_type} className="shrink-0" />
        {/* data-nepool-field: stable hook for the "Take me to next NEPOOL ID" guided-fill button */}
        <div data-nepool-field className="shrink-0">
          <InlineNepoolField
            value={array.nepool_gis_id}
            onSave={(v) => save({ nepool_gis_id: v })}
          />
        </div>
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          aria-label={expanded ? "Collapse array details" : "Expand array details"}
          aria-expanded={expanded}
          className="shrink-0 rounded p-1 text-zinc-400 transition-colors hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          <span aria-hidden className={`inline-block transition-transform ${expanded ? "rotate-90" : ""}`}>
            ▸
          </span>
        </button>
      </div>

      {expanded && (
        <div className="space-y-3 border-t border-cream-border bg-cream/40 px-3 py-3">
          {/* Possible-duplicate banner — surfaces another array on this
              tenant that shares NEPOOL ID, name, or utility accounts.
              One-click merge moves the duplicate's UAs under THIS array. */}
          <ArrayMergeSuggestionBanner
            arrayId={array.id}
            arrayName={array.name}
            onMerged={() => {
              // Signal the list to refresh so the merged-away row
              // disappears + UA counts update.
              window.dispatchEvent(new CustomEvent("so:arrays-changed"));
            }}
          />

          <div>
            <FieldLabel>Notes</FieldLabel>
            <EditableField
              value={array.notes}
              label="notes"
              onSave={(v) => save({ notes: v || null })}
              placeholder="—"
            />
          </div>

          {/* Generation type — lets an operator CORRECT the fuel after the
              fact. Autopop / import / sandbox create solar by default, so
              without this a wrong fuel was stuck routing to the solar (GMCS)
              writer with no recovery. Saves through the same updateArray patch. */}
          <div>
            <FieldLabel>Generation type</FieldLabel>
            <FuelPicker
              value={(array.fuel_type as FuelType) ?? DEFAULT_FUEL}
              label=""
              onChange={(f) => save({ fuel_type: f })}
            />
          </div>

          <div>
            <FieldLabel>
              {array.accounts.length} utility{" "}
              {array.accounts.length === 1 ? "account" : "accounts"}
            </FieldLabel>
            <UtilityAccountsPanel clientId={clientId} array={array} onChange={onChange} />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-cream-border pt-3">
            <label
              className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-zinc-500"
              title="Excluded arrays are hidden from reports and don't count toward billing (e.g. below the REC threshold)"
            >
              <input
                type="checkbox"
                checked={!!array.excluded}
                onChange={(e) => save({ excluded: e.target.checked })}
                className="h-3.5 w-3.5 accent-amber-500"
              />
              Hide from reports
            </label>
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                disabled={uploading}
                onClick={() => csvInputRef.current?.click()}
                className="rounded text-xs font-medium text-primary-600 transition-colors hover:text-primary-700 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
                title="Upload a daily generation CSV from GMP"
              >
                {uploading ? "Uploading…" : uploadedDays !== null ? `📊 ${uploadedDays} days` : "Upload CSV"}
              </button>
              <input
                ref={csvInputRef}
                type="file"
                accept=".csv"
                className="hidden"
                onChange={handleCsvUpload}
              />
              <button
                type="button"
                onClick={openSeModal}
                className="rounded text-xs font-medium text-primary-600 transition-colors hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
                title={seConnected ? "SolarEdge connected — manage connection" : "Connect SolarEdge for automatic daily data"}
              >
                {seConnected ? "📡 SolarEdge connected" : "Connect SolarEdge"}
              </button>
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="rounded text-xs font-medium text-zinc-400 transition-colors hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      <Modal
        open={confirmDelete}
        onClose={() => !deleting && setConfirmDelete(false)}
        title="Delete this array?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleDelete} disabled={deleting}>
              {deleting ? (
                <><Spinner /> Deleting…</>
              ) : (
                "Delete array"
              )}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          <span className="font-medium text-zinc-800">{array.name}</span> will be
          removed along with its {array.accounts.length} linked utility account
          {array.accounts.length === 1 ? "" : "s"}.{" "}
          <span className="font-medium text-zinc-800">You'll have 5 minutes to undo.</span>
        </p>
      </Modal>

      {/* SolarEdge connect modal */}
      <Modal
        open={seModalOpen}
        onClose={() => !seSaving && setSeModalOpen(false)}
        title={seConnected ? "SolarEdge connection" : "Connect SolarEdge"}
        footer={
          seConnected ? (
            <>
              <Button variant="ghost" onClick={() => setSeModalOpen(false)} disabled={seDisconnecting}>
                Close
              </Button>
              <Button variant="danger" onClick={handleSeDisconnect} disabled={seDisconnecting}>
                {seDisconnecting ? <><Spinner /> Disconnecting…</> : "Disconnect"}
              </Button>
            </>
          ) : seSites !== null ? (
            <>
              <Button variant="ghost" onClick={() => setSeSites(null)} disabled={seSaving}>
                Back
              </Button>
              <Button variant="primary" onClick={handleSeConnect} disabled={seSaving || seSelectedSiteId === ""}>
                {seSaving ? <><Spinner /> Connecting…</> : "Connect this site"}
              </Button>
            </>
          ) : (
            <>
              <Button variant="ghost" onClick={() => setSeModalOpen(false)} disabled={seSaving}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleSeConnect} disabled={seSaving || !seApiKey.trim()}>
                {seSaving ? <><Spinner /> Connecting…</> : "Connect"}
              </Button>
            </>
          )
        }
      >
        {seConnected ? (
          <div className="space-y-3">
            <p>
              This array is connected to SolarEdge site ID{" "}
              <span className="font-medium text-zinc-900">{array.solaredge_site_id}</span>.
              Daily generation is pulled automatically at 03:00 UTC.
            </p>
            {sePreview && (
              <p className="text-xs text-zinc-500">
                Last pull: {sePreview.days_pulled} days ·{" "}
                {sePreview.sample.slice(0, 3).map((s) => `${s.day}: ${s.kwh} kWh`).join(", ")}
              </p>
            )}
            <p className="text-xs text-zinc-400">
              Disconnecting removes the API key but keeps all historical data.
            </p>
          </div>
        ) : seSites !== null ? (
          <div className="space-y-3">
            {seSites.length === 0 ? (
              <>
                <p>
                  Your key appears to be a site-level key. Enter the SolarEdge site ID manually:
                </p>
                <Input
                  type="number"
                  placeholder="e.g. 1234567"
                  value={seSelectedSiteId === "" ? "" : String(seSelectedSiteId)}
                  onChange={(e) => setSeSelectedSiteId(e.target.value ? Number(e.target.value) : "")}
                  autoFocus
                />
              </>
            ) : (
              <>
                <p>Your account has {seSites.length} sites. Pick the one for this array:</p>
                <select
                  className="w-full rounded-lg border border-zinc-200 px-3 py-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  value={seSelectedSiteId}
                  onChange={(e) => setSeSelectedSiteId(Number(e.target.value))}
                >
                  {seSites.map((s) => (
                    <option key={s.site_id} value={s.site_id}>
                      {s.name} ({s.peak_kw} kW) — {s.address || "no address"}
                    </option>
                  ))}
                </select>
              </>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <p>
              Paste your SolarEdge API key. You can generate it in the SolarEdge portal
              under <span className="font-medium">Admin → Site Access</span> (site key) or{" "}
              <span className="font-medium">Account Settings</span> (account key covering all sites).
            </p>
            <Input
              type="text"
              placeholder="Paste API key here"
              value={seApiKey}
              onChange={(e) => setSeApiKey(e.target.value)}
              autoFocus
            />
            {sePreview && (
              <p className="rounded-lg bg-green-50 px-3 py-2 text-xs text-green-800">
                Pulled {sePreview.days_pulled} days ·{" "}
                {sePreview.sample.slice(0, 3).map((s) => `${s.day}: ${s.kwh} kWh`).join(", ")}
              </p>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

// ─── utility accounts under an array ───────────────────────────────────────

function UtilityAccountsPanel({
  clientId,
  array,
  onChange,
}: {
  clientId: number;
  array: ArrayRowT;
  onChange: (a: ArrayRowT) => void;
}) {
  const toast = useToast();
  const [adding, setAdding] = useState(false);

  async function remove(acctId: number) {
    try {
      await removeUtilityAccount(clientId, array.id, acctId);
      onChange({
        ...array,
        accounts: array.accounts.filter((a) => a.id !== acctId),
      });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't remove account");
    }
  }

  return (
    <div className="space-y-2">
      {array.accounts.length === 0 && !adding && (
        <p className="text-xs text-zinc-400">No utility accounts linked yet.</p>
      )}
      {array.accounts.map((acc) => (
        <div
          key={acc.id}
          className="flex items-center justify-between gap-3 rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm"
        >
          <div className="min-w-0">
            <span className="font-mono text-zinc-800">{acc.account_number}</span>
            <span className="ml-2 text-xs text-zinc-400">
              {acc.provider_label}
              {acc.nickname ? ` · ${acc.nickname}` : ""}
            </span>
          </div>
          <button
            type="button"
            onClick={() => remove(acc.id)}
            aria-label="Unlink account"
            className="rounded text-xs text-zinc-400 transition-colors hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
          >
            Unlink
          </button>
        </div>
      ))}

      {adding ? (
        <AddAccountRow
          clientId={clientId}
          arrayId={array.id}
          onCancel={() => setAdding(false)}
          onCreated={(acc) => {
            onChange({ ...array, accounts: [...array.accounts, acc] });
            setAdding(false);
          }}
        />
      ) : (
        <div>
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="rounded text-xs font-medium text-primary-600 transition-colors hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
          >
            + Link a utility account
          </button>
          <p className="mt-1.5 text-[11px] leading-snug text-zinc-400">
            Merging sub-meters? Link each utility account here to sum them into one
            array, then delete the individual array stubs.
          </p>
        </div>
      )}
    </div>
  );
}

function AddAccountRow({
  clientId,
  arrayId,
  onCancel,
  onCreated,
}: {
  clientId: number;
  arrayId: number;
  onCancel: () => void;
  onCreated: (acc: import("../lib/api").UtilityAccount) => void;
}) {
  const toast = useToast();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [provider, setProvider] = useState("gmp");
  const [accountNumber, setAccountNumber] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    listProviders()
      .then(setProviders)
      .catch(() => setProviders([{ code: "gmp", label: "Green Mountain Power (GMP)" }]));
  }, []);

  async function save() {
    if (!accountNumber.trim() || saving) return;
    setSaving(true);
    try {
      const acc = await addUtilityAccount(clientId, arrayId, {
        provider,
        account_number: accountNumber.trim(),
      });
      onCreated(acc);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't link account");
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-primary-200 bg-white p-3 sm:flex-row sm:items-end">
      <label className="flex-1">
        <span className="mb-1 block text-xs font-medium text-zinc-600">
          Provider
        </span>
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          className="w-full rounded-lg border border-zinc-300 bg-white px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
        >
          {providers.map((p) => (
            <option key={p.code} value={p.code}>
              {p.label}
              {p.scrape_status && p.scrape_status !== "live"
                ? " (manual — auto-capture coming)"
                : ""}
            </option>
          ))}
        </select>
      </label>
      <div className="flex-1">
        <Input
          id={`new-acct-${arrayId}`}
          label="Account number"
          placeholder="1234567890"
          value={accountNumber}
          onChange={(e) => setAccountNumber(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && save()}
        />
      </div>
      <div className="flex gap-2">
        <Button onClick={save} disabled={!accountNumber.trim() || saving} className="px-3 py-2">
          {saving ? <Spinner /> : "Link"}
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={saving} className="px-3 py-2">
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ─── add a new array (inline) ──────────────────────────────────────────────

function AddArrayRow({
  clientId,
  onCancel,
  onCreated,
}: {
  clientId: number;
  onCancel: () => void;
  onCreated: (a: ArrayRowT) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [gis, setGis] = useState("");
  const [gisError, setGisError] = useState("");
  const [saving, setSaving] = useState(false);
  // Fuel stays sticky across consecutive adds so an operator entering, say,
  // five wind turbines picks "Wind" once and keeps adding. Defaults to solar
  // so the common solar-only flow is one less decision.
  const [fuel, setFuel] = useState<FuelType>(DEFAULT_FUEL);
  const nameRef = useRef<HTMLInputElement | null>(null);

  async function save() {
    if (!name.trim() || saving) return;
    const gisTrimmed = gis.trim();
    if (gisTrimmed !== "" && !/^\d{5}$/.test(gisTrimmed)) {
      setGisError("NEPOOL IDs are 5 digits");
      return;
    }
    setSaving(true);
    try {
      const a = await createArray(clientId, {
        name: name.trim(),
        nepool_gis_id: gisTrimmed || null,
        // Only send a non-default fuel — keeps solar create payloads identical
        // to the pre-V2 shape.
        fuel_type: fuel === DEFAULT_FUEL ? undefined : fuel,
      });
      onCreated(a);
      toast.success(`Added ${a.name}`);
      // Stay open for bulk entry: clear the per-array fields, keep the fuel
      // selection, and refocus the name field. Fuel carries to the next array.
      setName("");
      setGis("");
      setGisError("");
      setSaving(false);
      nameRef.current?.focus();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't add array");
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-primary-200 bg-white p-3">
      <FuelPicker value={fuel} onChange={setFuel} label="Generation type" />
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
        <div className="flex-1">
          <Input
            ref={nameRef}
            id={`new-array-name-${clientId}`}
            label="Array name"
            autoFocus
            placeholder="South Field"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
            }}
          />
        </div>
        <div className="flex-1">
          <Input
            id={`new-array-gis-${clientId}`}
            label="NEPOOL-GIS ID"
            placeholder="53984"
            maxLength={5}
            value={gis}
            onChange={(e) => {
              const v = e.target.value.replace(/\D/g, "").slice(0, 5);
              setGis(v);
              if (gisError) setGisError("");
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") save();
            }}
          />
          {gisError ? (
            <p className="mt-0.5 text-[11px] text-red-600">{gisError}</p>
          ) : (
            <p className="mt-1 text-[11px] leading-snug text-zinc-400">
              5-digit ISO-NE asset ID — required to ship reports. Add it later if you
              don&apos;t have it now.
            </p>
          )}
        </div>
        <div className="flex gap-2">
          <Button onClick={save} disabled={!name.trim() || saving} className="px-3 py-2">
            {saving ? <Spinner /> : "Add"}
          </Button>
          <Button variant="ghost" onClick={onCancel} disabled={saving} className="px-3 py-2">
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="mb-0.5 block text-[11px] font-medium uppercase tracking-wide text-zinc-400">
      {children}
    </span>
  );
}
