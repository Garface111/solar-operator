import { useEffect, useState } from "react";
import { Button } from "../../ui/Button";
import { Modal } from "../../ui/Modal";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import { useDashboardContext } from "../../screens/DashboardLayout";
import {
  type NextRunPreview,
  type ClientRow,
  getNextRun,
  listClients,
  sendReportNow,
  patchSendMode,
} from "../../lib/api";

interface Props {
  /** Pre-fetched data. If omitted the component fetches on mount. */
  data?: NextRunPreview | null;
  /** Called after a successful send so the parent can refresh report history. */
  onSent?: () => void;
}

type SendModeValue = "to_me" | "to_client" | "to_both";

const SEND_MODES: { value: SendModeValue; label: string }[] = [
  { value: "to_me", label: "Just to me" },
  { value: "to_client", label: "Just to clients" },
  { value: "to_both", label: "Both" },
];

/** "Next run" countdown card with send-now action. */
export function NextRunCard({ data: prefetched, onSent }: Props) {
  const toast = useToast();
  const { account, patchAccount } = useDashboardContext();

  const [data, setData] = useState<NextRunPreview | null>(prefetched ?? null);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(prefetched === undefined);

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [clientList, setClientList] = useState<ClientRow[] | null>(null);
  const [loadingClients, setLoadingClients] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  const [sendMode, setSendMode] = useState<SendModeValue>(
    () => (account?.send_mode as SendModeValue) ?? "to_client",
  );

  // Sync with account once it loads (account starts null while fetching).
  useEffect(() => {
    if (account?.send_mode) {
      setSendMode(account.send_mode as SendModeValue);
    }
  }, [account?.send_mode]);

  useEffect(() => {
    if (prefetched !== undefined) return;
    getNextRun()
      .then(setData)
      .catch(() => setLoadError(true))
      .finally(() => setLoading(false));
  }, [prefetched]);

  useEffect(() => {
    if (!confirmOpen || clientList !== null) return;
    setLoadingClients(true);
    listClients()
      .then((rows) => {
        const active = rows.filter((c) => c.active);
        setClientList(active);
        setSelectedIds(new Set(active.map((c) => c.id)));
      })
      .catch((err) => {
        toast.error(err instanceof Error ? err.message : "Couldn't load clients");
        setClientList([]);
      })
      .finally(() => setLoadingClients(false));
  }, [confirmOpen, clientList, toast]);

  async function handleModeChange(mode: SendModeValue) {
    setSendMode(mode);
    patchAccount({ send_mode: mode });
    try {
      await patchSendMode(mode);
    } catch {
      // Silently ignore — the send-report call will re-save the mode anyway.
    }
  }

  async function doSend() {
    setSending(true);
    try {
      const ids = Array.from(selectedIds);
      const allSelected = clientList !== null && ids.length === clientList.length;
      const res = await sendReportNow(allSelected ? undefined : ids, sendMode);
      setConfirmOpen(false);
      const failures = res.results.filter((r) => !r.ok);
      if (res.client_count === 0) {
        toast.error("No active clients to send to — add a client first.");
      } else if (failures.length === 0) {
        toast.success(
          res.delivered === 1
            ? "Report sent to 1 client."
            : `Reports sent to ${res.delivered} clients.`,
        );
      } else if (res.delivered === 0) {
        toast.error(`All ${res.client_count} deliveries failed.`);
      } else {
        toast.error(
          `Sent to ${res.delivered} of ${res.client_count}. ${failures.length} failed.`,
        );
      }
      onSent?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't send reports");
    } finally {
      setSending(false);
    }
  }

  if (loading) {
    return (
      <div className="rounded-xl border border-cream-border bg-cream p-5 shadow-sm">
        <div className="h-4 w-44 animate-pulse rounded bg-zinc-200" />
        <div className="mt-2 h-3 w-72 animate-pulse rounded bg-zinc-100" />
      </div>
    );
  }

  if (loadError || !data) return null;

  const countdownLabel =
    data.days_until === 0
      ? "today"
      : data.days_until === 1
      ? "tomorrow"
      : `in ${data.days_until} days`;

  const preview = [
    `${data.array_count} ${data.array_count === 1 ? "array" : "arrays"}`,
    `${data.client_count} ${data.client_count === 1 ? "client" : "clients"}`,
    data.mwh_preview > 0
      ? `${data.mwh_preview.toFixed(2)} MWh captured so far`
      : null,
    data.rec_preview > 0
      ? `${data.rec_preview} RECs est.`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      <div className="rounded-xl border border-cream-border bg-cream p-5 shadow-sm">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-sm font-semibold text-zinc-900">
              Next run:{" "}
              <span className="text-primary-700">{countdownLabel}</span>
            </p>
            <p className="mt-1 text-xs text-zinc-500">
              Will include {preview}
            </p>
          </div>
          <div className="flex flex-col items-end gap-2">
            {/* Send mode — 3-segment toggle */}
            <div
              role="radiogroup"
              aria-label="Report recipients"
              className="flex rounded-xl border border-zinc-200 bg-zinc-50 p-1"
            >
              {SEND_MODES.map((m) => {
                const selected = sendMode === m.value;
                return (
                  <button
                    key={m.value}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => handleModeChange(m.value)}
                    className={[
                      "rounded-lg px-3 py-1 text-xs font-medium transition-colors",
                      "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                      selected
                        ? "bg-white text-zinc-900 shadow-sm"
                        : "text-zinc-500 hover:text-zinc-800",
                    ].join(" ")}
                  >
                    {m.label}
                  </button>
                );
              })}
            </div>
            <p className="text-[11px] text-zinc-500">
              Applies to this run and saves as your default.
            </p>
            <Button
              className="h-8 shrink-0 px-3 text-xs"
              onClick={() => setConfirmOpen(true)}
            >
              Send now
            </Button>
          </div>
        </div>
      </div>

      <Modal
        open={confirmOpen}
        onClose={() => {
          if (!sending) setConfirmOpen(false);
        }}
        title="Send reports now"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => setConfirmOpen(false)}
              disabled={sending}
            >
              Cancel
            </Button>
            <Button
              onClick={doSend}
              disabled={sending || selectedIds.size === 0}
            >
              {sending ? (
                <>
                  <Spinner />
                  Sending…
                </>
              ) : selectedIds.size === 0 ? (
                "Pick at least one client"
              ) : clientList && selectedIds.size === clientList.length ? (
                `Send to all ${clientList.length}`
              ) : (
                `Send to ${selectedIds.size} client${selectedIds.size === 1 ? "" : "s"}`
              )}
            </Button>
          </>
        }
      >
        {loadingClients && clientList === null ? (
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Spinner />
            Loading your clients…
          </div>
        ) : clientList === null || clientList.length === 0 ? (
          <p className="text-sm text-zinc-600">
            No active clients yet. Add a client first.
          </p>
        ) : (
          <div className="space-y-3">
            <p className="text-sm text-zinc-600">
              Each selected client gets this quarter's workbook by email.
            </p>
            <div className="flex items-center justify-between border-b border-zinc-100 pb-2 text-xs">
              <button
                type="button"
                onClick={() =>
                  setSelectedIds(new Set(clientList.map((c) => c.id)))
                }
                className="font-medium text-primary-600 hover:text-primary-700"
              >
                Select all
              </button>
              <button
                type="button"
                onClick={() => setSelectedIds(new Set())}
                className="font-medium text-zinc-500 hover:text-zinc-700"
              >
                Select none
              </button>
            </div>
            <ul className="max-h-64 space-y-1 overflow-y-auto pr-1">
              {clientList.map((c) => {
                const checked = selectedIds.has(c.id);
                return (
                  <li key={c.id}>
                    <label className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2 py-1.5 hover:bg-zinc-50">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={(e) => {
                          setSelectedIds((prev) => {
                            const next = new Set(prev);
                            if (e.target.checked) next.add(c.id);
                            else next.delete(c.id);
                            return next;
                          });
                        }}
                        className="h-4 w-4 accent-primary-600"
                      />
                      <span className="text-sm text-zinc-800">{c.name}</span>
                      {!c.contact_email && (
                        <span className="ml-auto text-[11px] text-amber-600">
                          no email
                        </span>
                      )}
                    </label>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </Modal>
    </>
  );
}
