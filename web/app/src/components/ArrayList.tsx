import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import { ArrayMergeSuggestionBanner } from "./ArrayMergeSuggestionBanner";
import {
  type ArrayRow as ArrayRowT,
  type Provider,
  listArrays,
  createArray,
  updateArray,
  deleteArray,
  bulkDeleteArrays,
  addUtilityAccount,
  removeUtilityAccount,
  listProviders,
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
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

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

  return (
    <div className="space-y-3">
      {/* Select mode header */}
      {arrays.length > 0 && (
        <div className="flex items-center justify-between">
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
      )}

      {arrays.length === 0 && !adding && (
        <p className="rounded-xl border border-dashed border-zinc-200 px-4 py-6 text-center text-sm text-zinc-400">
          No arrays yet. Arrays appear here once utility auto-populate runs, or add
          one manually.
        </p>
      )}

      {arrays.map((a, idx) => {
        const cascade =
          revealStartDelayMs !== undefined
            ? {
                className: "so-reveal-card",
                style: {
                  animationDelay: `${revealStartDelayMs + idx * 110}ms`,
                } as React.CSSProperties,
              }
            : null;
        return (
          <div key={a.id} {...(cascade ?? {})}>
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

      {adding ? (
        <AddArrayRow
          clientId={clientId}
          onCancel={() => setAdding(false)}
          onCreated={(a) => {
            addArrayLocal(a);
            setAdding(false);
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
          <ul className="mt-2 space-y-0.5 text-sm text-zinc-700">
            {arrays.filter((a) => selectedIds.has(a.id)).map((a) => (
              <li key={a.id} className="truncate">• {a.name}</li>
            ))}
          </ul>
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
  onSelect?: (id: number) => void;
}) {
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function save(patch: Partial<ArrayRowT>) {
    const updated = await updateArray(clientId, array.id, patch as any);
    onChange(updated);
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

  return (
    <div
      data-nepool-array-id={array.id}
      data-nepool-client-id={clientId}
      data-nepool-empty={!array.nepool_gis_id ? "true" : undefined}
      className={`rounded-xl border ${array.excluded ? "border-zinc-200 opacity-60" : "border-zinc-200"}${selected ? " ring-2 ring-primary-400" : ""}`}
    >
      <div className="grid grid-cols-1 gap-x-4 gap-y-2 p-3 sm:grid-cols-12 sm:items-center">
        {selectable && (
          <div className="sm:col-span-1 flex items-center">
            <input
              type="checkbox"
              checked={!!selected}
              onChange={() => onSelect?.(array.id)}
              aria-label={`Select ${array.name}`}
              className="h-4 w-4 accent-primary-500"
            />
          </div>
        )}
        <div className={selectable ? "sm:col-span-2" : "sm:col-span-3"}>
          <FieldLabel>Name</FieldLabel>
          <EditableField
            value={array.name}
            label="array name"
            onSave={(v) => save({ name: v })}
            emptyText="Unnamed array"
            className="font-medium"
          />
        </div>
        <div className="sm:col-span-3">
          <FieldLabel>NEPOOL-GIS ID</FieldLabel>
          <div className="flex items-center gap-2">
            {/* data-nepool-field: stable hook for the "Take me to next NEPOOL ID" guided-fill button */}
            <div data-nepool-field>
              <EditableField
                value={array.nepool_gis_id}
                label="NEPOOL-GIS ID"
                onSave={(v) => save({ nepool_gis_id: v || null })}
                placeholder="53984"
              />
            </div>
            {!array.nepool_gis_id && (
              <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                Add NEPOOL ID
              </span>
            )}
          </div>
          <p className="mt-1 text-[11px] leading-snug text-zinc-400">
            5-digit ISO-NE asset ID — required to ship reports. You can add it
            later if you don&apos;t have it now.
          </p>
        </div>
        <div className="sm:col-span-6">
          <FieldLabel>Notes</FieldLabel>
          <EditableField
            value={array.notes}
            label="notes"
            onSave={(v) => save({ notes: v || null })}
            placeholder="—"
          />
        </div>
      </div>

      <div className="flex items-center justify-between border-t border-zinc-100 px-3 py-2">
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="inline-flex items-center gap-1.5 rounded text-xs font-medium text-zinc-500 transition-colors hover:text-zinc-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
        >
          <span
            aria-hidden
            className={`transition-transform ${expanded ? "rotate-90" : ""}`}
          >
            ▸
          </span>
          {array.accounts.length} utility{" "}
          {array.accounts.length === 1 ? "account" : "accounts"}
        </button>
        <div className="flex items-center gap-3">
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-zinc-500" title="Excluded arrays are hidden from reports and don't count toward billing (e.g. below the REC threshold)">
            <input
              type="checkbox"
              checked={!!array.excluded}
              onChange={(e) => save({ excluded: e.target.checked })}
              className="h-3.5 w-3.5 accent-amber-500"
            />
            Hide from reports
          </label>
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            className="rounded text-xs font-medium text-zinc-400 transition-colors hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
          >
            Delete
          </button>
        </div>
      </div>

      {expanded && (
        <>
          {/* Possible-duplicate banner — surfaces another array on this
              tenant that shares NEPOOL ID, name, or utility accounts.
              One-click merge moves the duplicate's UAs under THIS array. */}
          <div className="px-3 pt-3">
            <ArrayMergeSuggestionBanner
              arrayId={array.id}
              arrayName={array.name}
              onMerged={() => {
                // Signal the list to refresh so the merged-away row
                // disappears + UA counts update.
                window.dispatchEvent(new CustomEvent("so:arrays-changed"));
              }}
            />
          </div>
          <UtilityAccounts
            clientId={clientId}
            array={array}
            onChange={onChange}
          />
        </>
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
    </div>
  );
}

// ─── utility accounts under an array ───────────────────────────────────────

function UtilityAccounts({
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
    <div className="space-y-2 border-t border-zinc-100 bg-zinc-50/60 px-3 py-3">
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
      });
      onCreated(a);
      toast.success(`Added ${a.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't add array");
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-primary-200 bg-white p-3 sm:flex-row sm:items-end">
      <div className="flex-1">
        <Input
          id={`new-array-name-${clientId}`}
          label="Array name"
          autoFocus
          placeholder="South Field"
          value={name}
          onChange={(e) => setName(e.target.value)}
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
          Cancel
        </Button>
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
