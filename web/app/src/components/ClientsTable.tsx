import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { EditableField } from "../ui/EditableField";
import { Toggle } from "../ui/Toggle";
import { useToast } from "../ui/Toast";
import { ArrayList } from "./ArrayList";
import { MergeSuggestionBanner } from "./MergeSuggestionBanner";

const AssignNepoolFromSpreadsheetModal = lazy(() =>
  import("./AssignNepoolFromSpreadsheetModal").then((m) => ({
    default: m.AssignNepoolFromSpreadsheetModal,
  })),
);
const ImportSpreadsheetModal = lazy(() =>
  import("./ImportSpreadsheetModal").then((m) => ({ default: m.ImportSpreadsheetModal })),
);
import {
  type ClientRow,
  type ArrayRow,
  type UtilityAccount,
  type ClientCreateInput,
  listArrays,
  updateClient,
  deleteClient,
  refreshCapture,
  sendClientReportToMe,
  downloadClientReport,
  mergeClientInto,
} from "../lib/api";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function deliveryStatus(c: ClientRow): { kind: "ok" | "bounced"; label: string } | null {
  const delivered = c.last_delivered_at ? new Date(c.last_delivered_at).getTime() : 0;
  const bounced = c.last_bounced_at ? new Date(c.last_bounced_at).getTime() : 0;
  if (!delivered && !bounced) return null;
  if (bounced >= delivered) {
    return {
      kind: "bounced",
      label: c.last_bounce_reason ? `Bounced: ${c.last_bounce_reason}` : "Bounced",
    };
  }
  return { kind: "ok", label: `Delivered ${new Date(c.last_delivered_at!).toLocaleDateString()}` };
}

function lastCaptureIso(c: ClientRow): string | null {
  const dates = [c.gmp_last_sync_at, c.vec_last_sync_at].filter(Boolean) as string[];
  if (!dates.length) return null;
  return dates.sort().pop() ?? null;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diffMs = Date.now() - new Date(iso).getTime();
  const days = Math.floor(diffMs / 86_400_000);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

/** Returns [{ util, count, maskedAccounts }] for chip display in the table row. */
function utilityChips(
  c: ClientRow,
  arrays: ArrayRow[] | undefined,
): Array<{ util: string; count: number; maskedAccounts: string[] }> {
  if (!arrays) {
    // No arrays loaded yet — fall back to login presence only
    const chips: Array<{ util: string; count: number; maskedAccounts: string[] }> = [];
    if (c.gmp_autopopulate || c.gmp_email || c.gmp_username) chips.push({ util: "GMP", count: 0, maskedAccounts: [] });
    if (c.vec_autopopulate || c.vec_email || c.vec_username) chips.push({ util: "VEC", count: 0, maskedAccounts: [] });
    return chips;
  }
  const byUtil = new Map<string, { count: number; masked: string[] }>();
  for (const arr of arrays) {
    for (const acct of arr.accounts) {
      const util = normalizeUtil(acct.provider, acct.provider_label);
      if (!byUtil.has(util)) byUtil.set(util, { count: 0, masked: [] });
      const entry = byUtil.get(util)!;
      entry.count++;
      const num = acct.account_number;
      const masked = num.length > 4 ? "···" + num.slice(-4) : num;
      if (!entry.masked.includes(masked)) entry.masked.push(masked);
    }
  }
  const ORDER = ["GMP", "VEC", "WEC"];
  return Array.from(byUtil.entries())
    .sort(([a], [b]) => ORDER.indexOf(a) - ORDER.indexOf(b))
    .map(([util, { count, masked }]) => ({ util, count, maskedAccounts: masked }));
}

function uniqueAccountCount(arrays: ArrayRow[]): number {
  const ids = new Set<number>();
  for (const arr of arrays) for (const a of arr.accounts) ids.add(a.id);
  return ids.size;
}

function normalizeUtil(provider: string, providerLabel: string): string {
  const p = provider.toUpperCase();
  if (p === "GMP" || p.includes("GREEN MOUNTAIN")) return "GMP";
  if (p === "VEC" || p.includes("VERMONT ELECTRIC") || p.includes("VEC")) return "VEC";
  if (p === "WEC") return "WEC";
  return providerLabel.slice(0, 3).toUpperCase();
}

function credentialForUtil(c: ClientRow, util: string): string | null {
  if (util === "GMP") return c.gmp_email || c.gmp_username || null;
  if (util === "VEC") return c.vec_email || c.vec_username || null;
  return null;
}

interface LoginGroup {
  util: string;
  credential: string | null;
  accounts: Array<{ account: UtilityAccount; arrays: ArrayRow[] }>;
}

function buildLoginGroups(client: ClientRow, arrays: ArrayRow[]): LoginGroup[] {
  // util → (accountId → {account, arrays[]})
  const byUtil = new Map<string, Map<number, { account: UtilityAccount; arrays: ArrayRow[] }>>();
  for (const arr of arrays) {
    for (const acct of arr.accounts) {
      const util = normalizeUtil(acct.provider, acct.provider_label);
      if (!byUtil.has(util)) byUtil.set(util, new Map());
      const acctMap = byUtil.get(util)!;
      if (!acctMap.has(acct.id)) acctMap.set(acct.id, { account: acct, arrays: [] });
      acctMap.get(acct.id)!.arrays.push(arr);
    }
  }
  const ORDER = ["GMP", "VEC", "WEC"];
  return Array.from(byUtil.entries())
    .sort(([a], [b]) => ORDER.indexOf(a) - ORDER.indexOf(b))
    .map(([util, acctMap]) => ({
      util,
      credential: credentialForUtil(client, util),
      accounts: Array.from(acctMap.values()),
    }));
}

// ─── Utility color themes ────────────────────────────────────────────────────

const UTIL_THEME: Record<string, { pill: string; border: string; bg: string }> = {
  GMP: { pill: "bg-emerald-100 text-emerald-800", border: "border-emerald-200", bg: "bg-emerald-50" },
  VEC: { pill: "bg-blue-100 text-blue-800", border: "border-blue-200", bg: "bg-blue-50" },
  WEC: { pill: "bg-amber-100 text-amber-800", border: "border-amber-200", bg: "bg-amber-50" },
};
const DEFAULT_UTIL_THEME = { pill: "bg-zinc-100 text-zinc-700", border: "border-zinc-200", bg: "bg-zinc-50" };

// ─── Small components ────────────────────────────────────────────────────────

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 14 14"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 transition-transform duration-200 ${expanded ? "rotate-90" : ""}`}
    >
      <polyline points="4,2 10,7 4,12" />
    </svg>
  );
}

function MoreMenu({
  client,
  downloading,
  onDownload,
  onDelete,
  onMerge,
}: {
  client: ClientRow;
  downloading: boolean;
  onDownload: () => void;
  onDelete: () => void;
  onMerge: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 focus:outline-none"
        title="More actions"
      >
        <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="3" cy="8" r="1.5" />
          <circle cx="8" cy="8" r="1.5" />
          <circle cx="13" cy="8" r="1.5" />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 top-7 z-30 min-w-[148px] rounded-lg border border-zinc-200 bg-white py-1 shadow-lg">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
              onDownload();
            }}
            disabled={downloading}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
          >
            {downloading && <Spinner className="h-3 w-3" />}
            Download .xlsx
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
              onMerge();
            }}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-zinc-700 hover:bg-zinc-50"
          >
            Merge into…
          </button>
          {client.active && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                onDelete();
              }}
              className="block w-full px-3 py-1.5 text-left text-xs text-red-600 hover:bg-red-50"
            >
              Delete client
            </button>
          )}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
              onDownload();
            }}
            disabled={downloading}
            className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
          >
            {downloading && <Spinner className="h-3 w-3" />}
            Download .xlsx
          </button>
        </div>
      )}
    </div>
  );
}

// ─── MergeIntoModal ──────────────────────────────────────────────────────────

interface MergeIntoModalProps {
  open: boolean;
  onClose: () => void;
  srcClient: ClientRow;
  srcArrays: ArrayRow[] | undefined;
  otherClients: ClientRow[];
  onMerged: (dst: ClientRow, undoToken: string) => void;
}

function MergeIntoModal({
  open,
  onClose,
  srcClient,
  srcArrays,
  otherClients,
  onMerged,
}: MergeIntoModalProps) {
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [selectedDst, setSelectedDst] = useState<ClientRow | null>(null);
  const [merging, setMerging] = useState(false);

  const srcArrayCount = srcArrays?.length ?? srcClient.array_count;
  const srcAcctCount = srcArrays != null ? uniqueAccountCount(srcArrays) : null;

  const filtered = otherClients.filter(
    (c) =>
      c.active &&
      c.name.toLowerCase().includes(query.toLowerCase()),
  );

  async function handleConfirm() {
    if (!selectedDst || merging) return;
    setMerging(true);
    try {
      const result = await mergeClientInto(srcClient.id, selectedDst.id);
      onMerged(result.dst_client, result.undo_token);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Merge failed");
    } finally {
      setMerging(false);
    }
  }

  if (!open) return null;

  return (
    <Modal
      open={open}
      onClose={() => !merging && onClose()}
      title={`Merge "${srcClient.name}" into…`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={merging}>Cancel</Button>
          <Button onClick={handleConfirm} disabled={!selectedDst || merging}>
            {merging ? <><Spinner /> Merging…</> : "Merge"}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-zinc-600">
          This will move{" "}
          <span className="font-medium text-zinc-800">
            {srcArrayCount} array{srcArrayCount === 1 ? "" : "s"}
            {srcAcctCount != null ? ` and ${srcAcctCount} utility account${srcAcctCount === 1 ? "" : "s"}` : ""}
          </span>{" "}
          from <span className="font-medium">{srcClient.name}</span> into the selected client.
          The source client will be removed. You'll have 1 hour to undo.
        </p>
        <input
          type="search"
          placeholder="Search clients…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
          autoFocus
        />
        <ul className="max-h-56 overflow-y-auto divide-y divide-zinc-100 rounded-lg border border-zinc-200">
          {filtered.length === 0 && (
            <li className="px-3 py-3 text-sm text-zinc-400">No clients found</li>
          )}
          {filtered.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => setSelectedDst(c)}
                className={[
                  "w-full px-3 py-2.5 text-left text-sm transition-colors hover:bg-zinc-50",
                  selectedDst?.id === c.id ? "bg-primary-50 text-primary-700 font-medium" : "text-zinc-800",
                ].join(" ")}
              >
                <span className="block truncate">{c.name}</span>
                {c.contact_email && (
                  <span className="block text-[11px] text-zinc-400 truncate">{c.contact_email}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
        {selectedDst && (
          <p className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-800">
            Will move into <span className="font-semibold">{selectedDst.name}</span>.
          </p>
        )}
      </div>
    </Modal>
  );
}

// ─── ClientsTable ─────────────────────────────────────────────────────────────

// Always render 9 <td> columns; checkbox cell is width-0 when selectMode is off.
const COLS = 9;

export interface ClientsTableProps {
  clients: ClientRow[];
  operatorEmail: string | null;
  expandClientId?: number;
  selectMode: boolean;
  selectedIds: Set<number>;
  onToggleSelect: (id: number) => void;
  onChange: (c: ClientRow) => void;
  onDeleted: (id: number, token: string, msg: string) => void;
  onUndo: (token: string, msg: string) => void;
  onOpenAddByLogin: () => void;
  /** All active clients in this tenant — used for the merge picker. */
  allClients: ClientRow[];
  /** Called after a successful merge with the undo token for the banner. */
  onMerged: (dstClient: ClientRow, srcId: number, undoToken: string) => void;
}

export function ClientsTable({
  clients,
  operatorEmail,
  expandClientId,
  selectMode,
  selectedIds,
  onToggleSelect,
  onChange,
  onDeleted,
  onUndo,
  onOpenAddByLogin,
  allClients,
  onMerged,
}: ClientsTableProps) {
  // Start with EVERY client row expanded — operators arriving here for the
  // first time need to immediately see that their arrays actually propagated
  // from the utility capture (the whole product promise hinges on "look,
  // your data is here"). A specific expandClientId from the route still
  // wins as a deep-link hint, but we never start collapsed.
  const initialExpanded = () => {
    const ids = new Set<number>(clients.map((c) => c.id));
    if (expandClientId != null) ids.add(expandClientId);
    return ids;
  };
  const [expandedIds, setExpandedIds] = useState<Set<number>>(initialExpanded);
  // Track rows ever opened so their panels stay mounted during close animation.
  const [everExpandedIds, setEverExpandedIds] = useState<Set<number>>(initialExpanded);

  const arraysCacheRef = useRef<Map<number, ArrayRow[]>>(new Map());
  const loadingIdsRef = useRef<Set<number>>(new Set());
  // Bump to force re-render after async cache writes.
  const [, setTick] = useState(0);

  // Pre-warm the array cache for every client so the expanded panels fill in
  // without a stagger of spinners. Fires once on mount; new clients added later
  // load lazily via toggleExpand.
  useEffect(() => {
    clients.forEach((c) => {
      if (arraysCacheRef.current.has(c.id) || loadingIdsRef.current.has(c.id)) return;
      loadingIdsRef.current.add(c.id);
      listArrays(c.id)
        .then((rows) => {
          arraysCacheRef.current.set(c.id, rows);
          loadingIdsRef.current.delete(c.id);
          setTick((t) => t + 1);
        })
        .catch(() => {
          arraysCacheRef.current.set(c.id, []);
          loadingIdsRef.current.delete(c.id);
          setTick((t) => t + 1);
        });
    });
    // Empty dep: we only want this once at mount. New clients fetch via toggleExpand.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function toggleExpand(id: number) {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
        setEverExpandedIds((e) => {
          const n = new Set(e);
          n.add(id);
          return n;
        });
        if (!arraysCacheRef.current.has(id) && !loadingIdsRef.current.has(id)) {
          loadingIdsRef.current.add(id);
          listArrays(id)
            .then((rows) => {
              arraysCacheRef.current.set(id, rows);
              loadingIdsRef.current.delete(id);
              setTick((t) => t + 1);
            })
            .catch(() => {
              arraysCacheRef.current.set(id, []);
              loadingIdsRef.current.delete(id);
              setTick((t) => t + 1);
            });
        }
      }
      return next;
    });
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-cream-border">
      <table className="w-full min-w-[680px] border-collapse text-xs">
        <thead>
          <tr className="sticky top-0 z-10 border-b border-cream-border bg-cream text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
            <th scope="col" className={selectMode ? "w-8 py-2.5 pl-3 pr-1" : "w-0 p-0"} />
            <th scope="col" className="w-8 py-2.5 pl-3 pr-1" />
            <th scope="col" className="py-2.5 pr-3 text-left">Client</th>
            <th scope="col" className="w-28 py-2.5 pr-3 text-left">Logins</th>
            <th scope="col" className="w-16 py-2.5 pr-3 text-right">Arrays</th>
            <th scope="col" className="w-16 py-2.5 pr-3 text-right">Accts</th>
            <th scope="col" className="w-28 py-2.5 pr-3 text-left">Last capture</th>
            <th scope="col" className="w-20 py-2.5 pr-3 text-left">Delivery</th>
            <th scope="col" className="w-24 py-2.5 pr-3 text-right">Actions</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-cream-border">
          {clients.map((client, i) => (
            <ClientTableRow
              key={client.id}
              client={client}
              isEven={i % 2 === 0}
              expanded={expandedIds.has(client.id)}
              everExpanded={everExpandedIds.has(client.id)}
              arrays={arraysCacheRef.current.get(client.id)}
              loadingArrays={loadingIdsRef.current.has(client.id)}
              onToggleExpand={() => toggleExpand(client.id)}
              operatorEmail={operatorEmail}
              selectMode={selectMode}
              selected={selectedIds.has(client.id)}
              onToggleSelect={onToggleSelect}
              onChange={onChange}
              onDeleted={(token, msg) => onDeleted(client.id, token, msg)}
              onUndo={onUndo}
              onOpenAddByLogin={onOpenAddByLogin}
              onArraysCacheInvalidate={() => {
                arraysCacheRef.current.delete(client.id);
                setTick((t) => t + 1);
              }}
              allClients={allClients.filter((c) => c.id !== client.id)}
              onMerged={onMerged}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── ClientTableRow ──────────────────────────────────────────────────────────

interface RowProps {
  client: ClientRow;
  isEven: boolean;
  expanded: boolean;
  everExpanded: boolean;
  arrays: ArrayRow[] | undefined;
  loadingArrays: boolean;
  onToggleExpand: () => void;
  operatorEmail: string | null;
  selectMode: boolean;
  selected: boolean;
  onToggleSelect: (id: number) => void;
  onChange: (c: ClientRow) => void;
  onDeleted: (token: string, msg: string) => void;
  onUndo: (token: string, msg: string) => void;
  onOpenAddByLogin: () => void;
  onArraysCacheInvalidate: () => void;
  allClients: ClientRow[];
  onMerged: (dstClient: ClientRow, srcId: number, undoToken: string) => void;
}

function ClientTableRow({
  client,
  isEven,
  expanded,
  everExpanded,
  arrays,
  loadingArrays,
  onToggleExpand,
  operatorEmail,
  selectMode,
  selected,
  onToggleSelect,
  onChange,
  onDeleted,
  onUndo,
  onOpenAddByLogin,
  onArraysCacheInvalidate,
  allClients,
  onMerged,
}: RowProps) {
  const toast = useToast();
  const [refreshing, setRefreshing] = useState(false);
  const [sendingToMe, setSendingToMe] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [mergeModalOpen, setMergeModalOpen] = useState(false);

  const delivery = deliveryStatus(client);
  const captureIso = lastCaptureIso(client);
  const acctCount = arrays != null ? uniqueAccountCount(arrays) : null;

  async function handleRefresh(e: React.MouseEvent) {
    e.stopPropagation();
    if (refreshing) return;
    setRefreshing(true);
    try {
      const updated = await refreshCapture(client.id);
      onChange(updated);
      toast.success(updated.gmp_last_sync_at ? "Refreshed" : "No captures yet");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't refresh");
    } finally {
      setRefreshing(false);
    }
  }

  async function handleSendToMe(e: React.MouseEvent) {
    e.stopPropagation();
    if (!operatorEmail || sendingToMe) return;
    setSendingToMe(true);
    try {
      await sendClientReportToMe(client.id, operatorEmail);
      toast.success(`Sent to ${operatorEmail}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Couldn't send";
      toast.error(
        msg.toLowerCase().includes("no bills")
          ? `No bills captured yet for ${client.name}`
          : msg,
      );
    } finally {
      setSendingToMe(false);
    }
  }

  async function handleDownload() {
    if (downloading) return;
    setDownloading(true);
    try {
      await downloadClientReport(client.id, client.name);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Couldn't download";
      toast.error(
        msg.toLowerCase().includes("no bills")
          ? `No bills captured yet for ${client.name}`
          : msg,
      );
    } finally {
      setDownloading(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      const res = await deleteClient(client.id);
      setConfirmDelete(false);
      onDeleted(res.undo_token, `Deleted ${client.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete");
    } finally {
      setDeleting(false);
    }
  }

  function handleRowClick(e: React.MouseEvent) {
    if ((e.target as HTMLElement).closest("button, a, input, select")) return;
    onToggleExpand();
  }

  const rowCls = [
    "cursor-pointer select-none transition-colors hover:bg-zinc-50/70",
    isEven ? "" : "bg-cream/40",
    expanded ? "bg-zinc-50/60" : "",
    selected ? "ring-2 ring-inset ring-primary-300" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <>
      <tr
        className={rowCls}
        onClick={handleRowClick}
        role="button"
        aria-expanded={expanded}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggleExpand();
          }
        }}
      >
        {/* Checkbox */}
        <td className={selectMode ? "w-8 py-2.5 pl-3 pr-1" : "w-0 overflow-hidden p-0"}>
          {selectMode && (
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelect(client.id)}
              onClick={(e) => e.stopPropagation()}
              aria-label={`Select ${client.name}`}
              className="h-3.5 w-3.5 accent-primary-500"
            />
          )}
        </td>

        {/* Chevron */}
        <td className="w-8 py-2.5 pl-3 pr-1 text-zinc-400">
          <ChevronIcon expanded={expanded} />
        </td>

        {/* Client name + contact email */}
        <td className="py-2.5 pr-3">
          <div className="flex items-center gap-1.5">
            <span
              className="max-w-[200px] truncate text-[13px] font-semibold text-primary-700"
              title={client.name}
            >
              {client.name || "Unnamed"}
            </span>
            {!client.active && (
              <span className="shrink-0 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[9px] font-medium text-zinc-500">
                inactive
              </span>
            )}
            {client.is_placeholder && (
              <span className="shrink-0 rounded-full bg-amber-100 px-1.5 py-0.5 text-[9px] font-medium text-amber-700">
                placeholder
              </span>
            )}
          </div>
          {client.contact_email && (
            <span
              className="block max-w-[200px] truncate text-[11px] text-zinc-400"
              title={client.contact_email}
            >
              {client.contact_email}
            </span>
          )}
        </td>

        {/* Logins — utility chips with per-utility account count */}
        <td className="w-28 py-2.5 pr-3">
          {(() => {
            const chips = utilityChips(client, arrays);
            if (chips.length === 0) return <span className="text-[11px] text-zinc-400">—</span>;
            const tooltip = chips
              .map((ch) => `${ch.util}: ${ch.maskedAccounts.join(", ") || "—"}`)
              .join(" | ");
            return (
              <div className="flex flex-wrap gap-1" title={tooltip}>
                {chips.map((ch) => {
                  const th = UTIL_THEME[ch.util] ?? DEFAULT_UTIL_THEME;
                  return (
                    <span
                      key={ch.util}
                      className={`inline-flex items-center rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${th.pill}`}
                    >
                      {ch.util}{ch.count > 0 ? `·${ch.count}` : ""}
                    </span>
                  );
                })}
              </div>
            );
          })()}
        </td>

        {/* Arrays */}
        <td className="w-16 py-2.5 pr-3 text-right font-mono text-[12px] tabular-nums text-zinc-700">
          {client.array_count}
        </td>

        {/* Accounts */}
        <td className="w-16 py-2.5 pr-3 text-right font-mono text-[12px] tabular-nums text-zinc-400">
          {acctCount != null ? acctCount : "—"}
        </td>

        {/* Last capture */}
        <td className="w-28 py-2.5 pr-3 text-[11px] text-zinc-500">
          <span title={captureIso ? new Date(captureIso).toLocaleString() : undefined}>
            {relativeTime(captureIso)}
          </span>
        </td>

        {/* Delivery badge */}
        <td className="w-20 py-2.5 pr-3">
          {delivery ? (
            <span
              className={[
                "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                delivery.kind === "ok"
                  ? "bg-emerald-50 text-emerald-700"
                  : "bg-red-50 text-red-700",
              ].join(" ")}
              title={delivery.label}
            >
              {delivery.kind === "ok" ? "✓" : "✕"}{" "}
              {delivery.kind === "ok" ? "OK" : "Bounced"}
            </span>
          ) : (
            <span className="text-zinc-300">—</span>
          )}
        </td>

        {/* Row actions */}
        <td className="w-24 py-2.5 pr-3">
          <div className="flex items-center justify-end gap-1">
            {/* Refresh */}
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing}
              title="Refresh capture status"
              className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 disabled:opacity-40 focus:outline-none"
            >
              {refreshing ? (
                <Spinner className="h-3 w-3" />
              ) : (
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <polyline points="23 4 23 10 17 10" />
                  <polyline points="1 20 1 14 7 14" />
                  <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
                </svg>
              )}
            </button>

            {/* Send to me */}
            <button
              type="button"
              onClick={handleSendToMe}
              disabled={sendingToMe || !operatorEmail}
              title={`Send report to ${operatorEmail ?? "your email"}`}
              className="flex h-6 w-6 items-center justify-center rounded text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 disabled:opacity-40 focus:outline-none"
            >
              {sendingToMe ? (
                <Spinner className="h-3 w-3" />
              ) : (
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="22" y1="2" x2="11" y2="13" />
                  <polygon points="22 2 15 22 11 13 2 9 22 2" />
                </svg>
              )}
            </button>

            {/* More menu (download + merge + delete) */}
            <MoreMenu
              client={client}
              downloading={downloading}
              onDownload={handleDownload}
              onDelete={() => setConfirmDelete(true)}
              onMerge={() => setMergeModalOpen(true)}
            />
          </div>
        </td>
      </tr>

      {/* Expansion row — stays mounted after first open to animate closed */}
      {everExpanded && (
        <tr>
          <td colSpan={COLS} className="p-0">
            <div
              className="grid transition-[grid-template-rows] duration-200 ease-out"
              style={{ gridTemplateRows: expanded ? "1fr" : "0fr" }}
            >
              <div className="overflow-hidden">
                <ExpandedPanel
                  client={client}
                  arrays={arrays}
                  loadingArrays={loadingArrays}
                  operatorEmail={operatorEmail}
                  onChange={onChange}
                  onDeleted={onDeleted}
                  onUndo={onUndo}
                  onOpenAddByLogin={onOpenAddByLogin}
                  onArraysCacheInvalidate={onArraysCacheInvalidate}
                />
              </div>
            </div>
          </td>
        </tr>
      )}

      {/* Delete confirmation */}
      <Modal
        open={confirmDelete}
        onClose={() => !deleting && setConfirmDelete(false)}
        title="Delete this client?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleDelete} disabled={deleting}>
              {deleting ? <><Spinner /> Deleting…</> : "Delete client"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          <span className="font-medium text-zinc-800">{client.name}</span> and all their arrays will
          be removed.{" "}
          <span className="font-medium text-zinc-800">You'll have 5 minutes to undo.</span>
        </p>
      </Modal>

      {/* Merge-into modal */}
      <MergeIntoModal
        open={mergeModalOpen}
        onClose={() => setMergeModalOpen(false)}
        srcClient={client}
        srcArrays={arrays}
        otherClients={allClients}
        onMerged={(dst, undoToken) => {
          setMergeModalOpen(false);
          onChange(dst);
          window.dispatchEvent(
            new CustomEvent("so:client-merged", { detail: { src: client.id, dst: dst.id } }),
          );
          onMerged(dst, client.id, undoToken);
        }}
      />
    </>
  );
}

// ─── ExpandedPanel ────────────────────────────────────────────────────────────

interface ExpandedPanelProps {
  client: ClientRow;
  arrays: ArrayRow[] | undefined;
  loadingArrays: boolean;
  operatorEmail: string | null;
  onChange: (c: ClientRow) => void;
  onDeleted: (token: string, msg: string) => void;
  onUndo: (token: string, msg: string) => void;
  onOpenAddByLogin: () => void;
  onArraysCacheInvalidate: () => void;
}

function ExpandedPanel({
  client,
  arrays,
  loadingArrays,
  operatorEmail,
  onChange,
  onDeleted,
  onUndo,
  onOpenAddByLogin,
  onArraysCacheInvalidate,
}: ExpandedPanelProps) {
  const toast = useToast();
  const [sendingToMe, setSendingToMe] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [assigningNepool, setAssigningNepool] = useState(false);
  const [importingArrays, setImportingArrays] = useState(false);
  const [arrayRefreshSignal, setArrayRefreshSignal] = useState(0);

  const gmpLogin = client.gmp_email || client.gmp_username || "";
  const vecLogin = client.vec_email || client.vec_username || "";
  const loginGroups = arrays != null ? buildLoginGroups(client, arrays) : [];
  const hasAnyLogin = !!(
    gmpLogin ||
    client.gmp_autopopulate ||
    vecLogin ||
    client.vec_autopopulate
  );

  async function patch(p: Partial<ClientCreateInput> & { active?: boolean }) {
    const updated = await updateClient(client.id, p);
    onChange(updated);
  }

  async function handleGmpToggle(v: boolean) {
    try {
      await patch({ gmp_autopopulate: v });
      toast.success(v ? "GMP auto-populate on" : "GMP auto-populate off");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't update");
    }
  }

  async function handleVecToggle(v: boolean) {
    try {
      await patch({ vec_autopopulate: v });
      toast.success(v ? "VEC auto-populate on" : "VEC auto-populate off");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't update");
    }
  }

  async function handleSendToMe() {
    if (!operatorEmail || sendingToMe) return;
    setSendingToMe(true);
    try {
      await sendClientReportToMe(client.id, operatorEmail);
      toast.success(`Sent to ${operatorEmail}. Check your inbox.`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Couldn't send report";
      toast.error(
        msg.toLowerCase().includes("no bills")
          ? `No bills captured yet — log into your utility portal as ${client.name}.`
          : msg,
      );
    } finally {
      setSendingToMe(false);
    }
  }

  async function handleDownload() {
    if (downloading) return;
    setDownloading(true);
    try {
      await downloadClientReport(client.id, client.name);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Couldn't download";
      toast.error(
        msg.toLowerCase().includes("no bills")
          ? `No bills captured yet for ${client.name}.`
          : msg,
      );
    } finally {
      setDownloading(false);
    }
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      const res = await deleteClient(client.id);
      setConfirmDelete(false);
      onDeleted(res.undo_token, `Deleted ${client.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete");
    } finally {
      setDeleting(false);
    }
  }

  async function reactivate() {
    try {
      await patch({ active: true });
      toast.success(`Reactivated ${client.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't reactivate");
    }
  }

  return (
    <div className="border-t border-cream-border bg-white px-6 py-5">
      {/* Merge suggestion banner */}
      <MergeSuggestionBanner
        client={client}
        onMerged={(dst, mergedFromId, undoToken) => {
          onChange(dst);
          window.dispatchEvent(
            new CustomEvent("so:client-merged", { detail: { src: mergedFromId, dst: dst.id } }),
          );
          onUndo(undoToken, `Merged "${dst.name}"`);
        }}
      />

      <div className="mt-1 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* ── Left: Login tree ── */}
        <div>
          <h4 className="mb-3 text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
            Logins &amp; accounts
          </h4>

          {loadingArrays && (
            <div className="flex items-center gap-2 text-xs text-zinc-400">
              <Spinner className="h-3 w-3" />
              Loading…
            </div>
          )}

          {!loadingArrays && !hasAnyLogin && (
            <div className="rounded-lg border border-dashed border-zinc-200 px-4 py-5 text-center">
              <p className="text-xs text-zinc-400">No utility logins connected yet.</p>
              <button
                type="button"
                onClick={onOpenAddByLogin}
                className="mt-2 text-xs font-medium text-primary-600 hover:text-primary-700 focus:outline-none"
              >
                + Connect a login
              </button>
            </div>
          )}

          {!loadingArrays && hasAnyLogin && loginGroups.length === 0 && (
            <div className="rounded-lg border border-dashed border-zinc-200 px-4 py-5 text-center">
              <p className="text-xs text-zinc-400">
                Login configured but no accounts captured yet.
              </p>
              <p className="mt-1 text-xs text-zinc-400">
                Open the utility portal signed in as{" "}
                <span className="font-medium">
                  {gmpLogin || vecLogin || "this client"}
                </span>{" "}
                to capture their accounts.
              </p>
            </div>
          )}

          {loginGroups.length > 0 && (
            <div className="max-h-[420px] space-y-2.5 overflow-y-auto pr-1">
              {loginGroups.map((group) => {
                const th = UTIL_THEME[group.util] ?? DEFAULT_UTIL_THEME;
                return (
                  <div
                    key={group.util}
                    className={`rounded-lg border ${th.border} ${th.bg} px-3 py-2.5`}
                  >
                    <div className="mb-2 flex items-center gap-2">
                      <span
                        className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${th.pill}`}
                      >
                        {group.util}
                      </span>
                      {group.credential ? (
                        <span className="text-[11px] text-zinc-500">
                          Signed in as{" "}
                          <span className="font-medium text-zinc-700">{group.credential}</span>
                        </span>
                      ) : (
                        <span className="text-[11px] text-zinc-400">No credential set</span>
                      )}
                      <span className="ml-auto text-[10px] text-zinc-400">
                        {group.accounts.length} account{group.accounts.length === 1 ? "" : "s"}
                      </span>
                    </div>
                    <LoginAccountList group={group} />
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* ── Right: Settings ── */}
        <div className="space-y-4">
          {/* Report actions */}
          <div className="rounded-xl border border-primary-100 bg-primary-50/50 px-4 py-3">
            <h4 className="text-[10px] font-semibold uppercase tracking-wide text-primary-700">
              Report
            </h4>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                variant="secondary"
                onClick={handleSendToMe}
                disabled={sendingToMe || !operatorEmail}
              >
                {sendingToMe ? <><Spinner /> Sending…</> : "Email it to me"}
              </Button>
              <button
                type="button"
                onClick={handleDownload}
                disabled={downloading}
                className="inline-flex items-center rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 focus:outline-none"
              >
                {downloading ? <><Spinner /> Downloading…</> : "Download .xlsx"}
              </button>
            </div>
          </div>

          {/* GMP auto-populate */}
          <div className="rounded-xl bg-zinc-50 px-4 py-3">
            <Toggle
              id={`gmp-autopop-${client.id}`}
              checked={client.gmp_autopopulate}
              onChange={handleGmpToggle}
              label="GMP — auto-populate arrays from portal"
            />
            {client.gmp_autopopulate && (
              <div className="mt-2">
                <span className="mb-1 block text-xs font-medium text-zinc-600">
                  GMP login (email or username)
                </span>
                <EditableField
                  value={gmpLogin}
                  label="GMP login"
                  onSave={(v) => {
                    const isEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
                    return patch({
                      gmp_email: v && isEmail ? v : null,
                      gmp_username: v && !isEmail ? v : null,
                    });
                  }}
                  emptyText="add GMP login"
                  placeholder="client@gmail.com or jdoe"
                />
              </div>
            )}
          </div>

          {/* VEC auto-populate */}
          <div className="rounded-xl bg-zinc-50 px-4 py-3">
            <Toggle
              id={`vec-autopop-${client.id}`}
              checked={client.vec_autopopulate}
              onChange={handleVecToggle}
              label="VEC — auto-populate arrays from portal"
            />
            {client.vec_autopopulate && (
              <div className="mt-2">
                <span className="mb-1 block text-xs font-medium text-zinc-600">
                  VEC login (email or username)
                </span>
                <EditableField
                  value={vecLogin}
                  label="VEC login"
                  onSave={(v) => {
                    const isEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
                    return patch({
                      vec_email: v && isEmail ? v : null,
                      vec_username: v && !isEmail ? v : null,
                    });
                  }}
                  emptyText="add VEC login"
                  placeholder="client@gmail.com or jdoe"
                />
              </div>
            )}
          </div>

          {/* Editable fields */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <span className="mb-1 block text-xs font-medium text-zinc-600">Contact email</span>
              <EditableField
                value={client.contact_email}
                label="contact email"
                type="email"
                onSave={(v) => patch({ contact_email: v || null })}
                emptyText="add contact email"
                placeholder="reports@client.org"
              />
            </div>
            <div>
              <span className="mb-1 block text-xs font-medium text-zinc-600">CC emails</span>
              <EditableField
                value={client.cc_emails}
                label="CC emails"
                onSave={(v) => patch({ cc_emails: v || null })}
                emptyText="none"
                placeholder="extra@example.com"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <span className="mb-1 block text-xs font-medium text-zinc-600">
                Report frequency
              </span>
              <select
                value={client.report_frequency ?? ""}
                onChange={(e) => patch({ report_frequency: e.target.value || null })}
                className="w-full rounded-xl border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
              >
                <option value="">Inherit from account</option>
                <option value="monthly">Monthly</option>
                <option value="quarterly">Quarterly</option>
              </select>
            </div>
            <div>
              <span className="mb-1 block text-xs font-medium text-zinc-600">Notes</span>
              <EditableField
                value={client.notes}
                label="notes"
                onSave={(v) => patch({ notes: v || null })}
                emptyText="—"
                placeholder="Internal notes"
              />
            </div>
          </div>
        </div>
      </div>

      {/* Arrays section */}
      <div className="mt-6">
        <div className="mb-2 flex items-center justify-between">
          <h4 className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
            Arrays
          </h4>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setImportingArrays(true)}
              className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-600 hover:border-zinc-400 hover:text-zinc-800 focus:outline-none"
              title="Upload a spreadsheet of arrays under this client"
            >
              Import arrays
            </button>
            <button
              type="button"
              onClick={() => setAssigningNepool(true)}
              className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-600 hover:border-zinc-400 hover:text-zinc-800 focus:outline-none"
            >
              Import NEPOOL IDs
            </button>
          </div>
        </div>
        <ArrayList
          clientId={client.id}
          refreshSignal={arrayRefreshSignal}
          onCountChange={(count) => {
            onChange({ ...client, array_count: count });
            onArraysCacheInvalidate();
          }}
          onUndo={onUndo}
        />
      </div>

      {/* Footer: delete / reactivate */}
      <div className="mt-4 flex justify-end">
        {client.active ? (
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            className="rounded text-xs font-medium text-zinc-400 hover:text-red-600 focus:outline-none"
          >
            Delete client
          </button>
        ) : (
          <button
            type="button"
            onClick={reactivate}
            className="rounded text-xs font-medium text-primary-600 hover:text-primary-700 focus:outline-none"
          >
            Reactivate client
          </button>
        )}
      </div>

      {/* Modals */}
      {assigningNepool && (
        <Suspense fallback={null}>
          <AssignNepoolFromSpreadsheetModal
            open={assigningNepool}
            onClose={() => setAssigningNepool(false)}
            onAssigned={() => {
              setArrayRefreshSignal((n) => n + 1);
              onArraysCacheInvalidate();
            }}
            clientId={client.id}
            clientName={client.name}
          />
        </Suspense>
      )}
      {importingArrays && (
        <Suspense fallback={null}>
          <ImportSpreadsheetModal
            open={importingArrays}
            onClose={() => setImportingArrays(false)}
            onImported={() => {
              setArrayRefreshSignal((n) => n + 1);
              onArraysCacheInvalidate();
            }}
            forceClientId={client.id}
            forceClientName={client.name}
          />
        </Suspense>
      )}
      <Modal
        open={confirmDelete}
        onClose={() => !deleting && setConfirmDelete(false)}
        title="Delete this client?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)} disabled={deleting}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleDelete} disabled={deleting}>
              {deleting ? <><Spinner /> Deleting…</> : "Delete client"}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          <span className="font-medium text-zinc-800">{client.name}</span> and all their arrays will
          be removed.{" "}
          <span className="font-medium text-zinc-800">You'll have 5 minutes to undo.</span>
        </p>
      </Modal>
    </div>
  );
}

// ── Collapsible login → accounts → arrays renderer ─────────────────────────
// When a client has many utility accounts and each has many arrays, the inline
// expansion can grow taller than the rest of the page. These two components
// show the first few items and a "+ N more" toggle so the row stays compact
// by default but every detail is one click away.

function LoginAccountList({ group }: { group: LoginGroup }) {
  const [expanded, setExpanded] = useState(false);
  const COLLAPSED_LIMIT = 3;
  const visible = expanded
    ? group.accounts
    : group.accounts.slice(0, COLLAPSED_LIMIT);
  const remainder = group.accounts.length - visible.length;
  return (
    <div className="space-y-2 pl-1">
      {visible.map(({ account, arrays: acctArrays }) => (
        <div key={account.id}>
          <p className="text-[11px] font-medium text-zinc-600">
            Account {account.account_number}
            {account.customer_number && (
              <span className="ml-2 font-normal text-zinc-400">
                cust #{account.customer_number}
              </span>
            )}
            {account.nickname && (
              <span className="ml-1.5 font-normal text-zinc-400">
                ({account.nickname})
              </span>
            )}
          </p>
          <AccountArraysList arrays={acctArrays} />
        </div>
      ))}
      {remainder > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="text-[11px] font-medium text-primary-600 hover:text-primary-700 focus:outline-none"
        >
          + {remainder} more account{remainder === 1 ? "" : "s"}
        </button>
      )}
      {expanded && group.accounts.length > COLLAPSED_LIMIT && (
        <button
          type="button"
          onClick={() => setExpanded(false)}
          className="text-[11px] font-medium text-zinc-400 hover:text-zinc-600 focus:outline-none"
        >
          Show less
        </button>
      )}
    </div>
  );
}

function AccountArraysList({ arrays }: { arrays: ArrayRow[] }) {
  const [expanded, setExpanded] = useState(false);
  const ARRAY_LIMIT = 3;
  const visible = expanded ? arrays : arrays.slice(0, ARRAY_LIMIT);
  const remainder = arrays.length - visible.length;
  return (
    <ul className="mt-0.5 space-y-0.5 pl-3">
      {visible.map((arr) => (
        <li key={arr.id} className="text-[10px] text-zinc-500">
          <span className="font-medium text-zinc-700">{arr.name}</span>
          {arr.nepool_gis_id && (
            <span className="ml-1.5 font-mono text-[9px] text-zinc-400">
              {arr.nepool_gis_id}
            </span>
          )}
        </li>
      ))}
      {remainder > 0 && (
        <li>
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="text-[10px] font-medium text-primary-600 hover:text-primary-700 focus:outline-none"
          >
            + {remainder} more array{remainder === 1 ? "" : "s"}
          </button>
        </li>
      )}
      {expanded && arrays.length > ARRAY_LIMIT && (
        <li>
          <button
            type="button"
            onClick={() => setExpanded(false)}
            className="text-[10px] font-medium text-zinc-400 hover:text-zinc-600 focus:outline-none"
          >
            Show less
          </button>
        </li>
      )}
    </ul>
  );
}
