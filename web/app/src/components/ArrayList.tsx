import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import {
  type ArrayRow as ArrayRowT,
  type Provider,
  listArrays,
  createArray,
  updateArray,
  deleteArray,
  addUtilityAccount,
  removeUtilityAccount,
  listProviders,
} from "../lib/api";

interface Props {
  clientId: number;
  onCountChange?: (count: number) => void;
}

export function ArrayList({ clientId, onCountChange }: Props) {
  const toast = useToast();
  const [arrays, setArrays] = useState<ArrayRowT[] | null>(null);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let cancelled = false;
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
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clientId]);

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
  }

  function removeArrayLocal(id: number) {
    setArrays((rows) => {
      const next = rows ? rows.filter((a) => a.id !== id) : rows;
      if (next) onCountChange?.(next.length);
      return next;
    });
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
      {arrays.length === 0 && !adding && (
        <p className="rounded-xl border border-dashed border-zinc-200 px-4 py-6 text-center text-sm text-zinc-400">
          No arrays yet. Arrays appear here once GMP auto-populate runs, or add
          one manually.
        </p>
      )}

      {arrays.map((a) => (
        <ArrayRow
          key={a.id}
          clientId={clientId}
          array={a}
          onChange={replaceArray}
          onDelete={removeArrayLocal}
        />
      ))}

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
    </div>
  );
}

// ─── one array row ─────────────────────────────────────────────────────────

function ArrayRow({
  clientId,
  array,
  onChange,
  onDelete,
}: {
  clientId: number;
  array: ArrayRowT;
  onChange: (a: ArrayRowT) => void;
  onDelete: (id: number) => void;
}) {
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);

  // Type-to-confirm: deleting an array is permanent and cascades to bills, so
  // the operator must type the exact name (unlike the reversible client deactivate).
  const confirmMatches = confirmText.trim() === array.name.trim();

  function closeConfirm() {
    if (deleting) return;
    setConfirmDelete(false);
    setConfirmText("");
  }

  async function save(patch: Partial<ArrayRowT>) {
    const updated = await updateArray(clientId, array.id, patch as any);
    onChange(updated);
  }

  async function handleDelete() {
    if (!confirmMatches || deleting) return;
    setDeleting(true);
    try {
      await deleteArray(clientId, array.id);
      onDelete(array.id);
      toast.success(`Deleted ${array.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete array");
      setDeleting(false);
      setConfirmDelete(false);
      setConfirmText("");
    }
  }

  return (
    <div className="rounded-xl border border-zinc-200">
      <div className="grid grid-cols-1 gap-x-4 gap-y-2 p-3 sm:grid-cols-12 sm:items-center">
        <div className="sm:col-span-4">
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
            <EditableField
              value={array.nepool_gis_id}
              label="NEPOOL-GIS ID"
              onSave={(v) => save({ nepool_gis_id: v || null })}
              placeholder="53984"
            />
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
        <div className="sm:col-span-2">
          <FieldLabel>Bill timing</FieldLabel>
          <select
            value={array.bill_offset_months === 0 ? "0" : "1"}
            onChange={(e) =>
              save({ bill_offset_months: Number(e.target.value) })
            }
            aria-label="Bill timing"
            className="w-full rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
          >
            <option value="1">Prior month (default)</option>
            <option value="0">Same month (sub-metered)</option>
          </select>
        </div>
        <div className="sm:col-span-3">
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
        <button
          type="button"
          onClick={() => setConfirmDelete(true)}
          className="rounded text-xs font-medium text-zinc-400 transition-colors hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
        >
          Delete array
        </button>
      </div>

      {expanded && (
        <UtilityAccounts
          clientId={clientId}
          array={array}
          onChange={onChange}
        />
      )}

      <Modal
        open={confirmDelete}
        onClose={closeConfirm}
        title="Delete this array?"
        footer={
          <>
            <Button variant="ghost" onClick={closeConfirm} disabled={deleting}>
              Cancel
            </Button>
            <Button
              variant="danger"
              onClick={handleDelete}
              disabled={deleting || !confirmMatches}
            >
              {deleting ? (
                <>
                  <Spinner />
                  Deleting…
                </>
              ) : (
                "Delete array"
              )}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          Type the array name to permanently delete{" "}
          <span className="font-medium text-zinc-800">{array.name}</span> and all
          its captured bills. This cannot be undone (unlike deactivating a
          client).
        </p>
        <p className="mt-2 text-xs text-zinc-500">
          Removes {array.accounts.length} linked utility account
          {array.accounts.length === 1 ? "" : "s"} and every bill tied to them.
        </p>
        <div className="mt-4">
          <Input
            id={`confirm-delete-${array.id}`}
            label="Array name"
            autoFocus
            placeholder={array.name}
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleDelete()}
          />
        </div>
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
            Merging sub-meters? Link each GMP account here to sum them into this
            one array (the Starlake case), then delete the duplicate arrays.
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
  const [offset, setOffset] = useState("1");
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!name.trim() || saving) return;
    setSaving(true);
    try {
      const a = await createArray(clientId, {
        name: name.trim(),
        nepool_gis_id: gis.trim() || null,
        bill_offset_months: Number(offset),
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
          value={gis}
          onChange={(e) => setGis(e.target.value)}
        />
        <p className="mt-1 text-[11px] leading-snug text-zinc-400">
          5-digit ISO-NE asset ID — required to ship reports. Add it later if you
          don&apos;t have it now.
        </p>
      </div>
      <div className="w-full sm:w-44">
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-zinc-600">
            Bill timing
          </span>
          <select
            value={offset === "0" ? "0" : "1"}
            onChange={(e) => setOffset(e.target.value)}
            className="w-full rounded-lg border border-zinc-300 bg-white px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
          >
            <option value="1">Prior month (default)</option>
            <option value="0">Same month (sub-metered)</option>
          </select>
        </label>
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
