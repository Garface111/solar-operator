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
import { Spinner } from "../ui/Spinner";
import { Modal } from "../ui/Modal";
import { EditableField } from "../ui/EditableField";
import { useToast } from "../ui/Toast";
import { ArrayList } from "./ArrayList";
import { AssignNepoolFromSpreadsheetModal } from "./AssignNepoolFromSpreadsheetModal";
import { ImportSpreadsheetModal } from "./ImportSpreadsheetModal";
import { MergeSuggestionBanner } from "./MergeSuggestionBanner";
import {
  type ClientRow,
  updateClient,
  deleteClient,
  sendClientReportToMe,
  downloadClientReport,
} from "../lib/api";



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

/** Human "Last sent" line for the REPORT section. Relative for recent sends
 *  (≤30 days), absolute date for anything older, explicit "Never sent" when
 *  we've never delivered to this client. */
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
  // is exactly the one we want — rename the client, watch arrays come in.
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
  const [sendingToMe, setSendingToMe] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [assigningNepool, setAssigningNepool] = useState(false);
  // Per-client array-import modal — the spreadsheet's operator_name column
  // is ignored; every row lands under THIS client. Complements the global
  // "Import spreadsheet" at the top of the page, which creates new clients.
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

  async function patch(p: Partial<ClientRow>) {
    const updated = await updateClient(client.id, p as any);
    onChange(updated);
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

  const delivery = deliveryStatus(client);

  return (
    <div
      className={`rounded-xl border bg-cream transition-shadow ${
        expanded ? "border-cream-border shadow-sm" : "border-cream-border"
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
        </div>

        <div className="hidden shrink-0 text-right text-xs text-zinc-400 sm:block">
          <RevealNumber value={client.array_count} delayMs={revealDelay(revealIndex, 0)} />{" "}
          {client.array_count === 1 ? "array" : "arrays"}
        </div>
      </div>

      {/* During the welcome reveal we eagerly mount the drawer (hidden)
          so ArrayList kicks off its listArrays() fetch in parallel with
          the cascade — otherwise rows arrive 200-500ms after the
          animation slot has passed and the "Loading arrays…" stub
          dead-ends the wave. */}
      {(expanded || reveal.active) && (
        <div
          className="border-t border-wood-300 px-4 py-4"
          style={!expanded ? { display: "none" } : undefined}
          aria-hidden={!expanded}
        >
          {/* Possible-duplicate banner — surfaces cross-provider matches
              the create-time dedup can't catch (e.g. same human on GMP
              under bruce@example.com + VEC under bgenereaux). One-click
              merge or persistent "Keep separate". */}
          <MergeSuggestionBanner
            client={client}
            onMerged={(dst, mergedFromId, undoToken) => {
              onChange(dst);
              // Sibling-card cleanup: parent reloads clients, but we
              // also emit a lightweight event so the soft-deleted card
              // disappears immediately without a flash.
              window.dispatchEvent(
                new CustomEvent("so:client-merged", {
                  detail: { src: mergedFromId, dst: dst.id },
                }),
              );
              // Bubble undo token up so ClientsSection can show the banner.
              onUndo?.(undoToken, `Merged "${client.name}" into "${dst.name}"`);
            }}
          />

          {/* 2-column layout: left = client status at a glance, right = the
              redesigned report sidebar (Ford's boxed region). */}
          <div className="mt-4 grid grid-cols-1 gap-6 sm:grid-cols-[1fr_280px]">
            {/* LEFT column — client status (the bulk content, the arrays
                table, lives full-width below this grid). */}
            <div className="space-y-4">
              <div className="flex flex-wrap gap-1.5">
                {client.active ? (
                  <Chip variant="emerald">Active</Chip>
                ) : (
                  <Chip variant="muted">Suspended</Chip>
                )}
                {delivery && (
                  <Chip variant={delivery.kind === "ok" ? "emerald" : "red"}>
                    {delivery.text}
                  </Chip>
                )}
              </div>
            </div>

            {/* RIGHT column — the redesigned report sidebar. Three labelled
                sub-sections (REPORT / DELIVERY / DATA) on a single 16px
                vertical rhythm, separated by wood-300 gold hairlines. */}
            <div className="rounded-xl border border-cream-border bg-cream p-4 sm:p-5">
              {/* ── Section 1: REPORT — outgoing report actions ── */}
              <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                Report
              </h4>
              <div className="mt-4 flex flex-col gap-2">
                <Button
                  variant="primary"
                  className="w-full"
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
                  className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-cream-border bg-white px-5 py-2.5 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
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
              <p className="mt-2 text-[11px] text-zinc-500">{lastSentLabel(client)}</p>

              {/* gold hairline */}
              <div className="my-4 border-t border-wood-300/60" />

              {/* ── Section 2: DELIVERY — who it goes to + how often ── */}
              <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                Delivery
              </h4>
              <div className="mt-4 space-y-2">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="w-20 shrink-0 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                    To
                  </span>
                  <div className="min-w-0">
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
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="w-20 shrink-0 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                    CC
                  </span>
                  <div className="min-w-0">
                    <EditableField
                      value={client.cc_emails}
                      label="CC emails"
                      onSave={(v) => patch({ cc_emails: v || null })}
                      emptyText="—"
                      placeholder="extra@example.com, other@example.com"
                    />
                  </div>
                </div>
                <div className="flex items-center gap-x-2">
                  <span className="w-20 shrink-0 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                    Frequency
                  </span>
                  <select
                    value={client.report_frequency ?? "quarterly"}
                    onChange={(e) =>
                      patch({ report_frequency: e.target.value || "quarterly" })
                    }
                    aria-label="Report frequency"
                    className="min-w-0 flex-1 rounded-lg border border-cream-border bg-white px-2.5 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
                  >
                    <option value="monthly">Monthly</option>
                    <option value="quarterly">Quarterly</option>
                  </select>
                </div>
              </div>

              {/* gold hairline */}
              <div className="my-4 border-t border-wood-300/60" />

              {/* ── Section 3: DATA — import + notes ── */}
              <h4 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
                Data
              </h4>
              <div className="mt-4 space-y-3">
                {/* Single "Import data" dropdown — replaces the old two separate buttons.
                    Dropdown approach: user intent (create vs. assign) stays clear in the menu
                    without changing the backend preview semantics yet. */}
                <div className="relative" ref={importDropdownRef} data-tour-step="5">
                  <button
                    type="button"
                    onClick={() => setImportDropdownOpen((o) => !o)}
                    className="inline-flex w-full items-center justify-between rounded-lg border border-cream-border bg-white px-3 py-1.5 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
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
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="w-20 shrink-0 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                    Notes
                  </span>
                  <div className="min-w-0">
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

          {/* Arrays — full width below the 2-col section */}
          <div className="mt-5">
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
              Arrays
            </h4>
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

          <div className="mt-4 flex justify-end">
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
