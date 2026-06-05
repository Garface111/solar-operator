import { useEffect, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { Chip } from "../ui/Chip";
import { RevealNumber } from "../ui/RevealNumber";
import { useReveal } from "./WelcomeReveal";

/** Helper to compute stagger delay for numeric reveals inside a card.
 *  Returns 0 when reveal is inactive — RevealNumber will snap instantly. */
function useRevealDelay() {
  const reveal = useReveal();
  return (cardIndex: number, slot = 0): number =>
    reveal.active ? reveal.delayFor(cardIndex, slot) : 0;
}
import { Toggle } from "../ui/Toggle";
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import { ArrayList } from "./ArrayList";
import { openPortalTab } from "../lib/openPortalTab";
import { AssignNepoolFromSpreadsheetModal } from "./AssignNepoolFromSpreadsheetModal";
import { ImportSpreadsheetModal } from "./ImportSpreadsheetModal";
import { ProductionChart } from "./clients/ProductionChart";
import {
  type ClientRow,
  listClients,
  updateClient,
  deleteClient,
  refreshCapture,
  sendClientReportToMe,
  downloadClientReport,
} from "../lib/api";
import { type PollerHandle, pollUntilChanged } from "../lib/poller";



function captureFreshness(iso: string | null): string {
  if (!iso) return "No captures yet";
  return `Last capture: ${new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })}`;
}

/** Most recent Resend-reported delivery outcome for this client, or null if
 *  we've never heard back about a send. Bounce wins ties (it's the alarming one). */
function deliveryStatus(
  c: ClientRow,
): { kind: "ok" | "bounced"; text: string } | null {
  const delivered = c.last_delivered_at
    ? new Date(c.last_delivered_at).getTime()
    : 0;
  const bounced = c.last_bounced_at ? new Date(c.last_bounced_at).getTime() : 0;
  if (!delivered && !bounced) return null;
  if (bounced >= delivered) {
    return {
      kind: "bounced",
      text: c.last_bounce_reason ? `Bounced: ${c.last_bounce_reason}` : "Bounced",
    };
  }
  return {
    kind: "ok",
    text: `Delivered ${new Date(c.last_delivered_at!).toLocaleDateString()}`,
  };
}

interface Props {
  client: ClientRow;
  operatorEmail: string | null;
  defaultExpanded?: boolean;
  onChange: (c: ClientRow) => void;
  onDeleted?: (token: string, message: string) => void;
  onUndo?: (token: string, message: string) => void;
  selectable?: boolean;
  selected?: boolean;
  onSelect?: (id: number) => void;
  /** Index in the client list — used by WelcomeReveal for staggered number fill. */
  revealIndex?: number;
}

export function ClientCard({
  client,
  operatorEmail,
  defaultExpanded,
  onChange,
  onDeleted,
  onUndo,
  selectable,
  selected,
  onSelect,
  revealIndex = 0,
}: Props) {
  const toast = useToast();
  const reveal = useReveal();
  const revealDelay = useRevealDelay();
  // Placeholder clients (the "Your first client" row dropped in by the
  // array-count-only onboarding path) auto-expand by default and get a
  // visual nudge: amber ring + "Rename this to your real client" hint.
  // The walkthrough also anchors here so the operator's first interaction
  // is exactly the one we want — rename, paste utility email, toggle autopop.
  const isPlaceholder = !!client.is_placeholder;
  const [expanded, setExpanded] = useState(!!defaultExpanded || isPlaceholder);

  // Welcome-reveal choreography: when the number-fill wave reaches THIS
  // card (per-index stagger from WelcomeReveal), pop the arrays section
  // open so the cascade flows down into the array list instead of
  // dead-ending at the collapsed header. Only auto-expands once per
  // reveal — manual toggling afterward is preserved.
  const autoExpandedByReveal = useRef(false);
  useEffect(() => {
    if (!reveal.active) return;
    if (autoExpandedByReveal.current) return;
    // Fire just after the number-fill begins for this card so the
    // expansion lands in the middle of the wave, not before it.
    const delay = reveal.delayFor(revealIndex, 1);
    const t = window.setTimeout(() => {
      autoExpandedByReveal.current = true;
      setExpanded(true);
    }, delay);
    return () => window.clearTimeout(t);
  }, [reveal.active, reveal, revealIndex]);

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [sendingToMe, setSendingToMe] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [assigningNepool, setAssigningNepool] = useState(false);
  // Per-client array-import modal — the spreadsheet's operator_name column
  // is ignored; every row lands under THIS client. Complements the global
  // "Import spreadsheet" at the top of the page, which creates new clients.
  const [importingArrays, setImportingArrays] = useState(false);
  const [arrayRefreshSignal, setArrayRefreshSignal] = useState(0);
  const pollerRef = useRef<PollerHandle | null>(null);

  useEffect(() => {
    return () => { pollerRef.current?.cancel(); };
  }, []);

  async function handleRefresh() {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const updated = await refreshCapture(client.id);
      onChange(updated);
      toast.success(
        updated.gmp_last_sync_at
          ? "Capture status refreshed"
          : "No captures received yet",
      );
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't refresh");
    } finally {
      setRefreshing(false);
    }
  }

  async function patch(p: Partial<ClientRow>) {
    const updated = await updateClient(client.id, p as any);
    onChange(updated);
  }

  async function toggleAutopop(v: boolean) {
    try {
      await patch({ gmp_autopopulate: v });
      toast.success(v ? "GMP auto-populate on" : "GMP auto-populate off");
      if (v) {
        pollerRef.current?.cancel();
        const clientId = client.id;
        const [p, handle] = pollUntilChanged(
          listClients,
          (prev, next) => {
            const a = prev.find((c) => c.id === clientId);
            const b = next.find((c) => c.id === clientId);
            if (!a || !b) return false;
            return (
              b.array_count > a.array_count ||
              b.gmp_last_sync_at !== a.gmp_last_sync_at
            );
          },
        );
        pollerRef.current = handle;
        p.then((newClients) => {
          if (!newClients) return;
          const updated = newClients.find((c) => c.id === clientId);
          if (updated) {
            onChange(updated);
            setArrayRefreshSignal((s) => s + 1);
          }
        });
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't update");
    }
  }

  async function toggleVecAutopop(v: boolean) {
    try {
      await patch({ vec_autopopulate: v });
      toast.success(v ? "VEC auto-populate on" : "VEC auto-populate off");
      if (v) {
        pollerRef.current?.cancel();
        const clientId = client.id;
        const [p, handle] = pollUntilChanged(
          listClients,
          (prev, next) => {
            const a = prev.find((c) => c.id === clientId);
            const b = next.find((c) => c.id === clientId);
            if (!a || !b) return false;
            return (
              b.array_count > a.array_count ||
              b.vec_last_sync_at !== a.vec_last_sync_at
            );
          },
        );
        pollerRef.current = handle;
        p.then((newClients) => {
          if (!newClients) return;
          const updated = newClients.find((c) => c.id === clientId);
          if (updated) {
            onChange(updated);
            setArrayRefreshSignal((s) => s + 1);
          }
        });
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't update");
    }
  }

  async function handleDelete() {
    setDeleting(true);
    try {
      const res = await deleteClient(client.id);
      setConfirmDelete(false);
      setDeleting(false);
      onDeleted?.(res.undo_token, `Deleted ${client.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't delete");
      setDeleting(false);
      setConfirmDelete(false);
    }
  }

  async function reactivate() {
    try {
      const updated = await updateClient(client.id, { active: true });
      onChange(updated);
      toast.success(`Reactivated ${client.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't reactivate");
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
      if (msg.toLowerCase().includes("no bills")) {
        toast.error(
          `No bills captured yet — log into your utility portal as ${client.name} so the extension can pull their data.`,
        );
      } else {
        toast.error(msg);
      }
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
      const msg = err instanceof Error ? err.message : "Couldn't download report";
      if (msg.toLowerCase().includes("no bills")) {
        toast.error(
          `No bills captured yet — log into your utility portal as ${client.name} so the extension can pull their data.`,
        );
      } else {
        toast.error(msg);
      }
    } finally {
      setDownloading(false);
    }
  }

  const gmpLogin = client.gmp_email || client.gmp_username || "";
  const vecLogin = client.vec_email || client.vec_username || "";
  const delivery = deliveryStatus(client);
  const THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000;
  const captureStale =
    client.gmp_autopopulate &&
    (!client.gmp_last_sync_at ||
      Date.now() - new Date(client.gmp_last_sync_at).getTime() > THIRTY_DAYS_MS);

  return (
    <div
      className={`rounded-xl border bg-white transition-shadow ${
        expanded ? "border-zinc-300 shadow-sm" : "border-zinc-200"
      } ${selected ? "ring-2 ring-primary-400" : ""} ${
        isPlaceholder ? "ring-2 ring-amber-300/70 border-amber-200" : ""
      }`}
    >
      {/* header row — entire row is clickable to toggle expansion. The
          `group` class lets the chevron react on hover even though hover
          is on the parent. */}
      <div
        data-tour-step="2"
        className="group flex cursor-pointer items-center gap-3 p-4 select-none hover:bg-zinc-50/60 rounded-xl"
        onClick={(e) => {
          // Don't toggle when clicking interactive children (inputs, buttons, links)
          const tag = (e.target as HTMLElement).tagName;
          if (tag === "INPUT" || tag === "BUTTON" || tag === "A" || tag === "SELECT") return;
          if ((e.target as HTMLElement).closest("button, a, input, select")) return;
          setExpanded((prev) => !prev);
        }}
        role="button"
        aria-expanded={expanded}
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setExpanded((prev) => !prev); } }}
      >
        {selectable && (
          <input
            type="checkbox"
            checked={!!selected}
            onChange={() => onSelect?.(client.id)}
            onClick={(e) => e.stopPropagation()}
            aria-label={`Select ${client.name}`}
            className="h-4 w-4 shrink-0 accent-primary-500"
          />
        )}
        {/* Expand/collapse affordance — circular button-shaped, always
            visible, hover-styled. Whole header row is still clickable
            (handler on the parent div) — this just makes the click target
            obvious to scanning eyes. */}
        <span
          aria-hidden
          className={`grid h-8 w-8 shrink-0 place-items-center rounded-full border border-zinc-200 bg-zinc-50 text-zinc-500 transition-all group-hover:border-zinc-300 group-hover:bg-zinc-100 group-hover:text-zinc-700 ${
            expanded ? "rotate-90 border-zinc-300 bg-zinc-100 text-zinc-700" : ""
          }`}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 14 14"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="4,2 10,7 4,12" />
          </svg>
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <EditableField
              value={client.name}
              label="client name"
              onSave={(v) => patch({ name: v })}
              emptyText="Unnamed client"
              className={`text-base font-semibold ${isPlaceholder ? "text-amber-700" : ""}`}
            />
            {isPlaceholder && (
              <Chip variant="amber">placeholder</Chip>
            )}
            {!client.active && (
              <Chip variant="muted">inactive</Chip>
            )}
          </div>
          {isPlaceholder && (
            <p className="mt-0.5 text-xs leading-snug text-amber-700">
              Rename this to your real client (and paste their utility-login email
              below) — then log into your utility portal and watch their arrays
              appear here automatically.
            </p>
          )}
          <div className="mt-0.5 text-sm text-zinc-500">
            <EditableField
              value={client.contact_email}
              label="contact email"
              type="email"
              onSave={(v) => patch({ contact_email: v || null })}
              emptyText="add contact email"
              placeholder="reports@client.org"
            />
          </div>
          {client.last_delivery_at && !delivery && (
            <div className="mt-0.5 text-xs text-zinc-400">
              Last sent:{" "}
              {new Date(client.last_delivery_at).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
              })}
            </div>
          )}
          {delivery && (
            <div className="mt-1 flex items-center gap-1.5 text-xs">
              <span
                aria-hidden
                className={
                  delivery.kind === "ok" ? "text-primary-600" : "text-red-600"
                }
              >
                {delivery.kind === "ok" ? "✓" : "✕"}
              </span>
              <span
                className={
                  delivery.kind === "ok" ? "text-zinc-500" : "text-red-600"
                }
              >
                {delivery.text}
              </span>
            </div>
          )}
          {client.gmp_autopopulate && (
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
              <span
                className={
                  client.gmp_last_sync_at ? "text-zinc-500" : "text-amber-600"
                }
              >
                {captureFreshness(client.gmp_last_sync_at)}
              </span>
              <button
                type="button"
                onClick={handleRefresh}
                disabled={refreshing}
                className="rounded font-medium text-primary-600 transition-colors hover:text-primary-700 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
              >
                {refreshing ? "Checking…" : "Check status"}
              </button>
            </div>
          )}
          {captureStale && (
            <div className="mt-1.5 flex flex-wrap items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800">
              {gmpLogin ? (
                <>
                  <span>
                    {!client.gmp_last_sync_at ? (
                      <>
                        First-time setup — sign in as{" "}
                        <span className="font-medium">{gmpLogin}</span> at their utility portal so
                        we can auto-detect their arrays
                      </>
                    ) : (
                      <>
                        Open portal signed in as{" "}
                        <span className="font-medium">{gmpLogin}</span> to refresh
                      </>
                    )}
                  </span>
                  <a
                    href="https://www.greenmountainpower.com/account/"
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={(e) => {
                      e.preventDefault();
                      void openPortalTab("https://www.greenmountainpower.com/account/");
                    }}
                    className="shrink-0 font-medium text-amber-900 underline underline-offset-2 hover:text-amber-700"
                  >
                    Open greenmountainpower.com ↗
                  </a>
                </>
              ) : (
                <span>Sign into your utility portal to refresh this client&apos;s data</span>
              )}
            </div>
          )}
        </div>

        <div className="hidden shrink-0 text-right text-xs text-zinc-400 sm:block">
          <RevealNumber value={client.array_count} delayMs={revealDelay(revealIndex, 0)} />{" "}
          {client.array_count === 1 ? "array" : "arrays"}
        </div>
      </div>

      {expanded && (
        <div className="space-y-5 border-t border-zinc-100 px-4 py-4">
          {/* Report section — pinned to top of drawer so it's the first thing you see */}
          <div className="rounded-xl border border-primary-100 bg-primary-50/50 px-4 py-3">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-primary-700">
              {client.last_delivery_at
                ? `Report — ${new Date(client.last_delivery_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`
                : "Report"}
            </h4>
            <p className="mt-1 text-xs text-zinc-600">
              Preview what you&apos;ll send {client.name} — without contacting them.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                variant="secondary"
                onClick={handleSendToMe}
                disabled={sendingToMe || !operatorEmail}
              >
                {sendingToMe ? (
                  <>
                    <Spinner />
                    Sending…
                  </>
                ) : (
                  "Email it to me"
                )}
              </Button>
              <button
                type="button"
                onClick={handleDownload}
                disabled={downloading}
                className="inline-flex items-center rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
              >
                {downloading ? (
                  <>
                    <Spinner />
                    Downloading…
                  </>
                ) : (
                  "Download .xlsx"
                )}
              </button>
            </div>
          </div>

          {/* Solar production chart */}
          <div className="rounded-xl border border-zinc-100 bg-white px-4 py-3">
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
              Production
            </h4>
            <ProductionChart clientId={client.id} />
          </div>

          {/* GMP auto-populate */}
          <div data-tour-step="3" className="rounded-xl bg-zinc-50 px-4 py-3">
            <div data-tour-step="4">
            <Toggle
              id={`autopop-${client.id}`}
              checked={client.gmp_autopopulate}
              onChange={toggleAutopop}
              label="GMP — auto-populate arrays from portal"
            />
            </div>
            {client.gmp_autopopulate && (
              <div className="mt-3">
                <span className="mb-1 block text-xs font-medium text-zinc-600">
                  GMP login (email or username)
                </span>
                <EditableField
                  value={gmpLogin}
                  label="GMP login"
                  onSave={(v) => {
                    // Route to the right column: email-shaped → gmp_email.
                    const looksLikeEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
                    return patch({
                      gmp_email: v && looksLikeEmail ? v : null,
                      gmp_username: v && !looksLikeEmail ? v : null,
                    });
                  }}
                  emptyText="add GMP login"
                  placeholder="client@gmail.com or jdoe"
                />
                <p className="mt-1.5 text-xs text-zinc-500">
                  The credential the client uses to sign in at
                  greenmountainpower.com. We use this to match captured bills to
                  this client.
                </p>
                {client.gmp_last_sync_at && (
                  <p className="mt-1 text-xs text-zinc-400">
                    {captureFreshness(client.gmp_last_sync_at)}
                  </p>
                )}
              </div>
            )}
          </div>

          {/* VEC auto-populate */}
          <div className="rounded-xl bg-zinc-50 px-4 py-3">
            <Toggle
              id={`vec-autopop-${client.id}`}
              checked={client.vec_autopopulate}
              onChange={toggleVecAutopop}
              label="VEC — auto-populate arrays from portal"
            />
            {client.vec_autopopulate && (
              <div className="mt-3">
                <span className="mb-1 block text-xs font-medium text-zinc-600">
                  VEC login (email or username)
                </span>
                <EditableField
                  value={vecLogin}
                  label="VEC login"
                  onSave={(v) => {
                    const looksLikeEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v);
                    return patch({
                      vec_email: v && looksLikeEmail ? v : null,
                      vec_username: v && !looksLikeEmail ? v : null,
                    });
                  }}
                  emptyText="add VEC login"
                  placeholder="client@gmail.com or jdoe"
                />
                <p className="mt-1.5 text-xs text-zinc-500">
                  The credential the client uses to sign in at
                  vermontelectric.coop. We use this to match captured bills to
                  this client.
                </p>
                {client.vec_last_sync_at && (
                  <p className="mt-1 text-xs text-zinc-400">
                    {captureFreshness(client.vec_last_sync_at)}
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Extra delivery fields — CC recipients, report cadence, notes */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <span className="mb-1 block text-xs font-medium text-zinc-600">
                CC emails
              </span>
              <EditableField
                value={client.cc_emails}
                label="CC emails"
                onSave={(v) => patch({ cc_emails: v || null })}
                emptyText="none"
                placeholder="extra@example.com, other@example.com"
              />
              <p className="mt-1 text-[11px] text-zinc-400">
                Comma-separated. These addresses get a copy of every report.
              </p>
            </div>
            <div>
              <span className="mb-1 block text-xs font-medium text-zinc-600">
                Report frequency
              </span>
              <select
                value={client.report_frequency ?? ""}
                onChange={(e) =>
                  patch({ report_frequency: e.target.value || null })
                }
                aria-label="Report frequency override"
                className="w-full rounded-xl border border-zinc-300 bg-white px-3.5 py-2.5 text-sm focus:outline-none focus:border-transparent focus:ring-2 focus:ring-primary-500/40"
              >
                <option value="">Inherit from account</option>
                <option value="monthly">Monthly</option>
                <option value="quarterly">Quarterly</option>
              </select>
              <p className="mt-1 text-[11px] text-zinc-400">
                Override the account-wide schedule for this client only.
              </p>
            </div>
          </div>

          <div>
            <span className="mb-1 block text-xs font-medium text-zinc-600">
              Notes
            </span>
            <EditableField
              value={client.notes}
              label="notes"
              onSave={(v) => patch({ notes: v || null })}
              emptyText="—"
              placeholder="Internal notes — not sent to the client"
            />
          </div>

          {/* arrays */}
          <div>
            <div className="mb-2 flex items-center justify-between">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                Arrays
              </h4>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setImportingArrays(true)}
                  className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-600 transition-colors hover:border-zinc-400 hover:text-zinc-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  title="Upload a spreadsheet of arrays under this client"
                >
                  Import arrays
                </button>
                <button
                  type="button"
                  data-tour-step="5"
                  onClick={() => setAssigningNepool(true)}
                  className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1 text-xs font-medium text-zinc-600 transition-colors hover:border-zinc-400 hover:text-zinc-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                >
                  Import NEPOOL IDs
                </button>
              </div>
            </div>
            <div data-tour-step="7">
              <ArrayList
                clientId={client.id}
                refreshSignal={arrayRefreshSignal}
                onCountChange={(count) => onChange({ ...client, array_count: count })}
                onUndo={onUndo}
                revealStartDelayMs={
                  reveal.active ? reveal.delayFor(revealIndex, 2) : undefined
                }
              />
            </div>
          </div>


          <div className="flex justify-end">
            {client.active ? (
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="rounded text-xs font-medium text-zinc-400 transition-colors hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
              >
                Delete client
              </button>
            ) : (
              <button
                type="button"
                onClick={reactivate}
                className="rounded text-xs font-medium text-primary-600 transition-colors hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
              >
                Reactivate client
              </button>
            )}
          </div>
        </div>
      )}

      <AssignNepoolFromSpreadsheetModal
        open={assigningNepool}
        onClose={() => setAssigningNepool(false)}
        onAssigned={() => setArrayRefreshSignal((s) => s + 1)}
        clientId={client.id}
        clientName={client.name}
      />

      <ImportSpreadsheetModal
        open={importingArrays}
        onClose={() => setImportingArrays(false)}
        onImported={() => setArrayRefreshSignal((s) => s + 1)}
        forceClientId={client.id}
        forceClientName={client.name}
      />

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
              {deleting ? (
                <>
                  <Spinner />
                  Deleting…
                </>
              ) : (
                "Delete client"
              )}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-600">
          <span className="font-medium text-zinc-800">{client.name}</span> and all
          their arrays will be removed.{" "}
          <span className="font-medium text-zinc-800">You'll have 5 minutes to undo.</span>
        </p>
      </Modal>
    </div>
  );
}
