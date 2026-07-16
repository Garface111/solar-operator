import { Suspense, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import { ArrayList } from "./ArrayList";
import { MergeSuggestionBanner } from "./MergeSuggestionBanner";
import { QuarterlyProgressChip } from "./QuarterlyProgressChip";

const AssignNepoolFromSpreadsheetModal = lazyWithRetry(() =>
  import("./AssignNepoolFromSpreadsheetModal").then((m) => ({
    default: m.AssignNepoolFromSpreadsheetModal,
  })),
);
const ImportSpreadsheetModal = lazyWithRetry(() =>
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
  recentReportQuarters,
} from "../lib/api";

const REPORT_QUARTERS = recentReportQuarters(8);

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

/** Human "Last sent" line for the REPORT section. Relative for recent sends
 *  (≤30 days), absolute date for anything older, explicit "Never sent" when
 *  we've never delivered to this client. Mirrors ClientCard's helper. */
function lastSentLabel(c: ClientRow): string {
  if (!c.last_delivered_at) return "Never sent";
  const then = new Date(c.last_delivered_at).getTime();
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days <= 0) return "Last sent: today";
  if (days === 1) return "Last sent: yesterday";
  if (days <= 30) return `Last sent: ${days} days ago`;
  return `Last sent: ${new Date(c.last_delivered_at).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })}`;
}

/** Two-letter initials for the report-card avatar circle. Empty string when
 *  the name yields nothing (callers fall back to a ✉ glyph). Mirrors the
 *  sandbox ClientNode helper so the two surfaces feel identical. */
function getInitials(name: string): string {
  return name
    .split(/\s+/)
    .filter((w) => w.length > 0)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
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
  arrays: ArrayRow[] | undefined,
): Array<{ util: string; count: number; maskedAccounts: string[] }> {
  if (!arrays) return [];
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

/** For a Cloud-Capture client eagerly created from a stored login and still
 *  awaiting its first harvested bill (no arrays yet), the stored login lives on
 *  the gmp/vec login columns rather than on any Array. Surface it so the operator
 *  sees WHICH login the "Pulling bills…" card is tied to. The vec_* columns are
 *  shared across the whole SmartHub co-op family, so we can't name the exact
 *  co-op from the row alone — label it generically. */
function pendingLoginHint(c: ClientRow): { util: string; login: string } | null {
  const gmp = c.gmp_email || c.gmp_username;
  if (gmp) return { util: "GMP", login: gmp };
  const vec = c.vec_email || c.vec_username;
  if (vec) return { util: "Utility", login: vec };
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
  GMP: { pill: "bg-emerald-100 text-emerald-600", border: "border-emerald-200", bg: "bg-emerald-50" },
  VEC: { pill: "bg-sky-100 text-sky-800", border: "border-sky-200", bg: "bg-sky-50" },
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
  // Start collapsed by default — operators land on a tidy list and choose what
  // to drill into. An `expandClientId` from the route still wins as a deep-link
  // hint (e.g. coming back from a report screen) so we honor that.
  const initialExpanded = () => {
    const ids = new Set<number>();
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

  // Listens for the guided-fill button in the NEPOOL banner. If a user manually
  // collapsed a client row, "Take me to next NEPOOL ID" dispatches this event so
  // we can re-expand it before the button tries to scroll to the hidden element.
  useEffect(() => {
    function handleExpandClient(e: Event) {
      const id = (e as CustomEvent<{ clientId: number }>).detail.clientId;
      setExpandedIds((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
      });
      setEverExpandedIds((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
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
    window.addEventListener("so:nepool:expand-client", handleExpandClient);
    return () => window.removeEventListener("so:nepool:expand-client", handleExpandClient);
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
    <div className="relative rounded-xl border border-cream-border">
      {/* Gradient edge on mobile to signal horizontal scroll. Hidden on sm+
          where the table fits or the wider viewport makes it obvious. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-y-0 right-0 z-10 w-10 rounded-r-xl bg-gradient-to-l from-cream/80 to-transparent sm:hidden"
      />
      <div className="overflow-x-auto rounded-xl">
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
            const chips = utilityChips(arrays);
            if (chips.length === 0) {
              // Pending Cloud-Capture client — no arrays yet, but we know the
              // login it will pull from. Show it muted so the card isn't blank.
              const hint = client.capture_pending ? pendingLoginHint(client) : null;
              if (hint) {
                return (
                  <span
                    className="inline-flex max-w-[110px] items-center gap-1 rounded-full bg-zinc-100 px-1.5 py-0.5 text-[9px] font-medium text-zinc-500"
                    title={`${hint.util}: ${hint.login}`}
                  >
                    <span className="truncate">{hint.login}</span>
                  </span>
                );
              }
              return <span className="text-[11px] text-zinc-400">—</span>;
            }
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
          {client.capture_pending && client.array_count === 0 ? (
            <span className="text-zinc-300">·</span>
          ) : (
            client.array_count
          )}
        </td>

        {/* Accounts */}
        <td className="w-16 py-2.5 pr-3 text-right font-mono text-[12px] tabular-nums text-zinc-400">
          {acctCount != null ? acctCount : "—"}
        </td>

        {/* Last capture */}
        <td className="w-28 py-2.5 pr-3 text-[11px] text-zinc-500">
          {client.capture_pending && client.array_count === 0 ? (
            <span
              className="inline-flex items-center gap-1 rounded-full bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700"
              title="We're signing into the utility portal and pulling this client's bills. This usually lands within a minute or two."
            >
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
              Pulling bills…
            </span>
          ) : (
            <span title={captureIso ? new Date(captureIso).toLocaleString() : undefined}>
              {relativeTime(captureIso)}
            </span>
          )}
        </td>

        {/* Delivery badge */}
        <td className="w-20 py-2.5 pr-3">
          {delivery ? (
            <span
              className={[
                "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                delivery.kind === "ok"
                  ? "bg-emerald-50 text-emerald-600"
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
  const [reportQuarter, setReportQuarter] = useState(REPORT_QUARTERS[0]?.value ?? "");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [assigningNepool, setAssigningNepool] = useState(false);
  const [importingArrays, setImportingArrays] = useState(false);
  const [importDropdownOpen, setImportDropdownOpen] = useState(false);
  const [arrayRefreshSignal, setArrayRefreshSignal] = useState(0);
  const importDropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!importDropdownOpen) return;
    function handleClickOutside(e: MouseEvent) {
      if (importDropdownRef.current && !importDropdownRef.current.contains(e.target as Node)) {
        setImportDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [importDropdownOpen]);

  const loginGroups = arrays != null ? buildLoginGroups(client, arrays) : [];

  async function patch(p: Partial<ClientCreateInput> & { active?: boolean }) {
    const updated = await updateClient(client.id, p);
    onChange(updated);
  }

  async function handleSendToMe() {
    if (!operatorEmail || sendingToMe) return;
    setSendingToMe(true);
    try {
      await sendClientReportToMe(client.id, operatorEmail, reportQuarter || undefined);
      const qLabel = REPORT_QUARTERS.find((q) => q.value === reportQuarter)?.label ?? reportQuarter;
      toast.success(`Sent ${qLabel} report to ${operatorEmail}. Check your inbox.`);
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
      await downloadClientReport(client.id, client.name, reportQuarter || undefined);
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

          <div className="mb-3">
            <QuarterlyProgressChip
              clientId={client.id}
              onSendReports={handleSendToMe}
            />
          </div>

          {loadingArrays && (
            <div className="flex items-center gap-2 text-xs text-zinc-400">
              <Spinner className="h-3 w-3" />
              Loading…
            </div>
          )}

          {!loadingArrays && loginGroups.length === 0 && (
            <div className="rounded-lg border border-dashed border-zinc-200 px-4 py-5 text-center">
              <p className="text-xs text-zinc-400">No accounts captured yet.</p>
              <button
                type="button"
                onClick={onOpenAddByLogin}
                className="mt-2 text-xs font-medium text-primary-600 hover:text-primary-700 focus:outline-none"
              >
                + Connect a login
              </button>
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

        {/* ── Right: the redesigned report card (Ford's boxed region).
            Wears the sandbox client-card visual language — rounded-2xl white
            shell with a soft layered shadow, an avatar header strip, and the
            REPORT / DELIVERY / DATA sub-sections rendered as nested
            "login-row"-style themed cards. Mirrors ClientCard.tsx exactly.
            mt offset on desktop only so the card's top edge aligns with
            the left column's first content row. ── */}
        <div className="so-node-enter rounded-2xl border-[1.5px] border-zinc-300 bg-white p-4 shadow-[0_4px_14px_-2px_rgba(15,23,42,0.12),0_2px_4px_-1px_rgba(15,23,42,0.06)] transition-all duration-150 hover:border-zinc-400 hover:shadow-[0_8px_24px_-4px_rgba(15,23,42,0.16),0_3px_6px_-1px_rgba(15,23,42,0.08)] sm:p-5 lg:mt-7">
          {/* ── Header strip — sandbox avatar + micro-label pattern ── */}
          <div className="mb-3 flex items-center gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary-50 text-xs font-bold text-primary-700 select-none">
              {getInitials(client.name) || "✉"}
            </div>
            <div className="min-w-0 flex-1">
              <span className="block text-[10px] font-semibold uppercase tracking-wider text-primary-600/80 select-none">
                Report card
              </span>
              <p className="truncate text-sm font-semibold text-zinc-900">
                Quarterly delivery
              </p>
            </div>
            <span className="shrink-0 text-[11px] text-zinc-400">
              {lastSentLabel(client)}
            </span>
          </div>

          {/* ── Sub-card 1: REPORT (emerald) — outgoing report actions.
              Styled like a sandbox "Login" row: caret + colored dot + label. ── */}
          <div className="mb-2 rounded-xl border border-emerald-100 bg-emerald-50 p-2.5">
            <div className="mb-2 flex items-center gap-1.5">
              <svg className="h-3 w-3 shrink-0 rotate-90 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5} aria-hidden>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
              <span className="h-2 w-2 rounded-full bg-emerald-400" />
              <span className="text-[10px] font-semibold uppercase tracking-wider text-emerald-700">
                Report
              </span>
            </div>
            <div className="flex flex-col gap-2">
              <label className="flex items-center gap-2 rounded-md bg-white/70 px-2 py-1.5">
                <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  Quarter
                </span>
                <select
                  value={reportQuarter}
                  onChange={(e) => setReportQuarter(e.target.value)}
                  className="min-w-0 flex-1 rounded-md border border-emerald-100 bg-white px-2 py-1 text-sm font-medium text-zinc-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  aria-label="Report quarter"
                >
                  {REPORT_QUARTERS.map((q) => (
                    <option key={q.value} value={q.value}>
                      {q.label}
                    </option>
                  ))}
                </select>
              </label>
              <Button
                variant="primary"
                className="w-full"
                onClick={handleSendToMe}
                disabled={sendingToMe || !operatorEmail}
              >
                {sendingToMe ? <><Spinner /> Sending…</> : "Email it to me"}
              </Button>
              <button
                type="button"
                onClick={handleDownload}
                disabled={downloading}
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-cream-border bg-white px-5 py-2.5 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
              >
                {downloading ? <><Spinner /> Downloading…</> : "Download .xlsx"}
              </button>
              <Link
                to={`/verify/${client.id}`}
                className="inline-flex w-full items-center justify-center gap-1.5 rounded-xl border border-emerald-100 bg-white/60 px-5 py-2 text-sm font-medium text-emerald-700 transition-colors hover:bg-emerald-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
              >
                Verify accuracy →
              </Link>
            </div>
          </div>

          {/* ── Sub-card 2: DELIVERY (amber) — who it goes to + how often.
              Body rows are bg-white/70 chips, echoing sandbox account rows. ── */}
          <div className="mb-2 rounded-xl border border-amber-100 bg-amber-50 p-2.5">
            <div className="mb-2 flex items-center gap-1.5">
              <svg className="h-3 w-3 shrink-0 rotate-90 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5} aria-hidden>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
              <span className="h-2 w-2 rounded-full bg-amber-400" />
              <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-700">
                Delivery
              </span>
              <span className="ml-auto rounded-full bg-white/70 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                {(client.report_frequency ?? "quarterly") === "monthly" ? "Monthly" : "Quarterly"}
              </span>
            </div>
            <div className="space-y-1.5">
              <div className="flex items-baseline gap-2 rounded-md bg-white/70 px-2 py-1.5">
                <span className="w-14 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  To
                </span>
                <div className="min-w-0 flex-1">
                  <EditableField
                    value={client.contact_email}
                    label="contact email"
                    type="email"
                    onSave={(v) => patch({ contact_email: v || null })}
                    emptyText="add email"
                    placeholder="reports@client.org"
                  />
                </div>
              </div>
              <div className="flex items-baseline gap-2 rounded-md bg-white/70 px-2 py-1.5">
                <span className="w-14 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  CC
                </span>
                <div className="min-w-0 flex-1">
                  <EditableField
                    value={client.cc_emails}
                    label="CC emails"
                    onSave={(v) => patch({ cc_emails: v || null })}
                    emptyText="—"
                    placeholder="extra@example.com, other@example.com"
                  />
                </div>
              </div>
              <div className="flex items-center gap-2 rounded-md bg-white/70 px-2 py-1.5">
                <span className="w-14 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                  Every
                </span>
                <select
                  value={client.report_frequency ?? "quarterly"}
                  onChange={(e) =>
                    patch({ report_frequency: e.target.value || "quarterly" })
                  }
                  aria-label="Report frequency"
                  className="min-w-0 flex-1 rounded-md border border-amber-100 bg-white px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-amber-500/40"
                >
                  <option value="monthly">Monthly</option>
                  <option value="quarterly">Quarterly</option>
                </select>
              </div>
            </div>
          </div>

          {/* ── Sub-card 3: DATA (sky) — import + notes ── */}
          <div className="rounded-xl border border-sky-100 bg-sky-50 p-2.5">
            <div className="mb-2 flex items-center gap-1.5">
              <svg className="h-3 w-3 shrink-0 rotate-90 text-sky-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5} aria-hidden>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
              </svg>
              <span className="h-2 w-2 rounded-full bg-sky-400" />
              <span className="text-[10px] font-semibold uppercase tracking-wider text-sky-700">
                Data
              </span>
            </div>
            <div className="space-y-2">
            {/* Single "Import data" dropdown — replaces the two separate
                "Import arrays" + "Import NEPOOL IDs" buttons that used to
                live in the Arrays header. Same pattern as ClientCard. */}
            <div className="relative" ref={importDropdownRef}>
              <button
                type="button"
                onClick={() => setImportDropdownOpen((o) => !o)}
                className="inline-flex w-full items-center justify-between rounded-lg border border-sky-100 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
              >
                <span>Import data</span>
                <svg
                  width="12"
                  height="12"
                  viewBox="0 0 12 12"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden
                  className={`transition-transform ${importDropdownOpen ? "rotate-180" : ""}`}
                >
                  <polyline points="2,4 6,8 10,4" />
                </svg>
              </button>
              {importDropdownOpen && (
                <div className="absolute right-0 z-10 mt-1 w-full overflow-hidden rounded-xl border border-cream-border bg-white shadow-md">
                  <button
                    type="button"
                    onClick={() => {
                      setImportDropdownOpen(false);
                      setImportingArrays(true);
                    }}
                    className="block w-full px-3 py-2 text-left hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  >
                    <span className="text-sm font-medium text-zinc-700">Import arrays</span>
                    <span className="mt-0.5 block text-[11px] text-zinc-400">
                      Creates new arrays from a spreadsheet
                    </span>
                  </button>
                  <div className="border-t border-cream-border" />
                  <button
                    type="button"
                    onClick={() => {
                      setImportDropdownOpen(false);
                      setAssigningNepool(true);
                    }}
                    className="block w-full px-3 py-2 text-left hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  >
                    <span className="text-sm font-medium text-zinc-700">Import NEPOOL IDs</span>
                    <span className="mt-0.5 block text-[11px] text-zinc-400">
                      Fills IDs on existing arrays — no new rows created
                    </span>
                  </button>
                </div>
              )}
            </div>
            <div className="flex items-baseline gap-2 rounded-md bg-white/70 px-2 py-1.5">
              <span className="w-14 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-zinc-500">
                Notes
              </span>
              <div className="min-w-0 flex-1">
                <EditableField
                  value={client.notes}
                  label="notes"
                  onSave={(v) => patch({ notes: v || null })}
                  emptyText="—"
                  placeholder="Internal notes — not sent to the client"
                />
              </div>
            </div>
            </div>
          </div>
        </div>
      </div>

      {/* Arrays section — import actions moved into the right-column DATA
          dropdown ("Import data ▾"), so the header is just the label now. */}
      <div className="mt-6">
        <div className="mb-2">
          <h4 className="text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
            Arrays
          </h4>
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
            className="min-h-[44px] rounded px-2 text-xs font-medium text-zinc-400 hover:text-red-600 focus:outline-none"
          >
            Delete client
          </button>
        ) : (
          <button
            type="button"
            onClick={reactivate}
            className="min-h-[44px] rounded px-2 text-xs font-medium text-primary-600 hover:text-primary-700 focus:outline-none"
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
