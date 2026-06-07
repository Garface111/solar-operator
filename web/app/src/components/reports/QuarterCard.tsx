import { useState, useEffect } from "react";
import { Chip } from "../../ui/Chip";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import type { ClientRow } from "../../lib/api";
import { resendClientReport, downloadClientReport, updateClient } from "../../lib/api";

type Status = "draft" | "ready" | "sent" | "empty";

export interface QuarterCardProps {
  label: string;
  quarter: string;
  status: Status;
  arrayCount: number;
  lastDeliveredAt: string | null;
  mwhTotal: number;
  clients: ClientRow[];
  /** Fully controlled from ReportsTab (expand-all / collapse-all live there). */
  expanded: boolean;
  onToggle: () => void;
  /** Filters per-client table rows by name or email. */
  searchQuery?: string;
  onRefresh?: () => void;
}

const STATUS_CONFIG: Record<Status, { label: string; chipVariant: "emerald" | "muted" }> = {
  sent:  { label: "Sent",        chipVariant: "emerald" },
  ready: { label: "Ready",       chipVariant: "emerald" },
  draft: { label: "In progress", chipVariant: "muted"   },
  empty: { label: "No data",     chipVariant: "muted"   },
};

function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year:
      new Date(iso).getFullYear() !== new Date().getFullYear()
        ? "numeric"
        : undefined,
  });
}

export function clientDeliveryStatus(
  c: ClientRow,
): "bounced" | "sent" | "no_email" | "pending" {
  if (!c.contact_email) return "no_email";
  if (c.last_bounced_at) {
    const bouncedMs = new Date(c.last_bounced_at).getTime();
    const deliveredMs = c.last_delivered_at
      ? new Date(c.last_delivered_at).getTime()
      : 0;
    if (bouncedMs > deliveredMs) return "bounced";
  }
  if (c.last_delivered_at) return "sent";
  return "pending";
}

// ─── Recipient table row ──────────────────────────────────────────────────────

function ClientTableRow({
  client,
  quarter,
  highlight,
  index,
  onRefresh,
}: {
  client: ClientRow;
  quarter: string;
  highlight: boolean;
  index: number;
  onRefresh?: () => void;
}) {
  const toast = useToast();
  const [sending, setSending] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [emailDraft, setEmailDraft] = useState("");
  const [savingEmail, setSavingEmail] = useState(false);
  const deliveryStatus = clientDeliveryStatus(client);

  async function handleResend() {
    setSending(true);
    try {
      const res = await resendClientReport(client.id);
      toast.success(`Sent to ${res.recipient}.`);
      onRefresh?.();
    } catch (err) {
      toast.error(
        err instanceof Error ? `Couldn't resend — ${err.message}` : "Couldn't resend.",
      );
    } finally {
      setSending(false);
    }
  }

  async function handleDownload() {
    setDownloading(true);
    try {
      await downloadClientReport(client.id, client.name, quarter);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }

  async function saveEmail() {
    const v = emailDraft.trim();
    if (!v || savingEmail) return;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
      toast.error("That doesn't look like an email address.");
      return;
    }
    setSavingEmail(true);
    try {
      await updateClient(client.id, { contact_email: v });
      toast.success(`Saved email for ${client.name}.`);
      setEmailDraft("");
      onRefresh?.();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't save email");
    } finally {
      setSavingEmail(false);
    }
  }

  const statusChip = {
    sent:     <Chip variant="emerald">Sent</Chip>,
    bounced:  <Chip variant="red">Bounced</Chip>,
    no_email: <Chip variant="amber">No email</Chip>,
    pending:  <Chip variant="muted">Pending</Chip>,
  }[deliveryStatus];

  return (
    <tr
      className={[
        "so-row-in border-b border-zinc-100 last:border-0",
        highlight ? "so-row-highlight" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{ animationDelay: `${index * 50}ms` }}
    >
      {/* Client + email */}
      <td className="py-2 pl-4 pr-3 align-top">
        <div className="text-sm text-zinc-800">{client.name}</div>
        {client.contact_email ? (
          <a
            href={`mailto:${client.contact_email}`}
            className="text-[11px] text-zinc-400 underline-offset-2 hover:text-zinc-600 hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            {client.contact_email}
          </a>
        ) : (
          <span className="inline-flex items-center gap-1 align-middle">
            <input
              type="email"
              value={emailDraft}
              onChange={(e) => setEmailDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void saveEmail();
                }
              }}
              placeholder="add email…"
              disabled={savingEmail}
              className="w-36 rounded border border-amber-300 bg-amber-50/40 px-1.5 py-0.5 text-[11px] text-amber-900 placeholder:text-amber-400/70 focus:border-amber-500 focus:outline-none"
              aria-label={`Contact email for ${client.name}`}
            />
            {emailDraft.trim() && (
              <button
                type="button"
                onClick={() => void saveEmail()}
                disabled={savingEmail}
                className="rounded bg-amber-500 px-1.5 py-0.5 text-[11px] font-medium text-white hover:bg-amber-600 disabled:opacity-50"
              >
                {savingEmail ? <Spinner className="h-3 w-3" /> : "Save"}
              </button>
            )}
          </span>
        )}
        {deliveryStatus === "bounced" && client.last_bounce_reason && (
          <div className="mt-0.5 text-[11px] text-red-500">
            {client.last_bounce_reason}
          </div>
        )}
      </td>
      {/* Delivered date */}
      <td className="py-2 pr-3 align-top text-[11px] whitespace-nowrap text-zinc-400">
        {deliveryStatus === "sent" && client.last_delivered_at
          ? shortDate(client.last_delivered_at)
          : "—"}
      </td>
      {/* Status chip */}
      <td className="py-2 pr-2 align-top">{statusChip}</td>
      {/* Actions */}
      <td className="py-2 pr-4 align-top">
        <div className="flex gap-1">
          <button
            type="button"
            disabled={downloading}
            onClick={handleDownload}
            title="Download .xlsx"
            className="min-h-[32px] rounded px-2 py-1 text-[11px] text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 disabled:opacity-50"
          >
            {downloading ? <Spinner className="h-3 w-3" /> : ".xlsx"}
          </button>
          <button
            type="button"
            disabled={sending || deliveryStatus === "no_email"}
            onClick={handleResend}
            title="Re-send report"
            className="min-h-[32px] rounded px-2 py-1 text-[11px] text-zinc-400 hover:bg-zinc-100 hover:text-primary-600 disabled:opacity-50"
          >
            {sending ? <Spinner className="h-3 w-3" /> : "↩ send"}
          </button>
        </div>
      </td>
    </tr>
  );
}

// ─── QuarterCard ─────────────────────────────────────────────────────────────

export function QuarterCard({
  label,
  quarter,
  status,
  arrayCount,
  lastDeliveredAt: _lastDeliveredAt,
  mwhTotal,
  clients,
  expanded,
  onToggle,
  searchQuery = "",
  onRefresh,
}: QuarterCardProps) {
  const [highlightBounced, setHighlightBounced] = useState(false);
  const { label: statusLabel, chipVariant } = STATUS_CONFIG[status];

  // Compute delivery summary from current client state.
  const bouncedClients = clients.filter(
    (c) => clientDeliveryStatus(c) === "bounced",
  );
  const sentCount = clients.filter(
    (c) => clientDeliveryStatus(c) === "sent",
  ).length;

  // Clear highlight after animation completes.
  useEffect(() => {
    if (!highlightBounced) return;
    const t = setTimeout(() => setHighlightBounced(false), 1600);
    return () => clearTimeout(t);
  }, [highlightBounced]);

  function handleBounceStripClick() {
    if (!expanded) onToggle();
    setHighlightBounced(true);
  }

  // Build stat line: "4 clients · 21 arrays · 142.30 MWh · 142 RECs · ✓ 4 sent"
  const statParts: string[] = [
    clients.length > 0
      ? `${clients.length} ${clients.length === 1 ? "client" : "clients"}`
      : "",
    arrayCount > 0
      ? `${arrayCount} ${arrayCount === 1 ? "array" : "arrays"}`
      : "",
    mwhTotal > 0 ? `${mwhTotal.toFixed(2)} MWh` : "",
    mwhTotal > 0 ? `${Math.floor(mwhTotal)} RECs` : "",
    status === "draft" && mwhTotal === 0 ? "generating…" : "",
    sentCount > 0 ? `✓ ${sentCount} sent` : "",
  ].filter(Boolean);

  // Filter clients for the expanded table.
  const q = searchQuery.toLowerCase();
  const filteredClients = q
    ? clients.filter(
        (c) =>
          c.name.toLowerCase().includes(q) ||
          (c.contact_email ?? "").toLowerCase().includes(q),
      )
    : clients;

  return (
    <div className="rounded-xl border border-cream-border bg-cream">
      {/* Header */}
      <div className="flex items-center justify-between px-4 pb-1.5 pt-3">
        <span className="text-sm font-semibold text-zinc-900">{label}</span>
        <Chip variant={chipVariant}>{statusLabel}</Chip>
      </div>

      {/* Stat line */}
      {statParts.length > 0 && (
        <p className="px-4 pb-2 text-xs text-zinc-500">
          {statParts.join(" · ")}
        </p>
      )}

      {/* Bounce strip — click expands and highlights bounced rows. */}
      {bouncedClients.length > 0 && (
        <button
          type="button"
          onClick={handleBounceStripClick}
          className="mx-4 mb-2 flex w-[calc(100%-2rem)] items-start gap-1.5 rounded-lg border-l-2 border-red-300 bg-red-50/60 px-3 py-1.5 text-left hover:bg-red-50"
        >
          <span className="mt-0.5 flex-shrink-0 text-[11px] text-red-600">⚠</span>
          <span className="text-[11px] text-red-700">
            {bouncedClients.length === 1 ? "1 bounced" : `${bouncedClients.length} bounced`}
            {" — "}
            {bouncedClients
              .map(
                (c) =>
                  c.name +
                  (c.last_bounce_reason ? ` (${c.last_bounce_reason})` : ""),
              )
              .join(", ")}
          </span>
        </button>
      )}

      {/* Recipients toggle + table */}
      {clients.length > 0 && (
        <>
          <button
            type="button"
            onClick={onToggle}
            className="flex w-full items-center gap-1.5 border-t border-cream-border px-4 py-2 text-left text-[11px] font-medium text-zinc-400 hover:text-zinc-600"
          >
            <span className="text-[10px]">{expanded ? "▴" : "▸"}</span>
            {expanded ? "Hide recipients" : "Show recipients"}
          </button>

          {expanded && (
            <div className="overflow-x-auto border-t border-cream-border">
              {filteredClients.length === 0 ? (
                <p className="px-4 py-3 text-xs text-zinc-400">
                  No clients match &ldquo;{searchQuery}&rdquo;.
                </p>
              ) : (
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-zinc-100">
                      <th className="px-4 pb-1.5 pt-2 text-left text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
                        Client
                      </th>
                      <th className="pb-1.5 pr-3 pt-2 text-left text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
                        Sent
                      </th>
                      <th className="pb-1.5 pr-2 pt-2 text-left text-[10px] font-semibold uppercase tracking-wide text-zinc-400">
                        Status
                      </th>
                      <th className="pb-1.5 pr-4 pt-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {filteredClients.map((c, i) => (
                      <ClientTableRow
                        key={c.id}
                        client={c}
                        quarter={quarter}
                        highlight={
                          highlightBounced &&
                          clientDeliveryStatus(c) === "bounced"
                        }
                        index={i}
                        onRefresh={onRefresh}
                      />
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
