import { useState, useCallback } from "react";
import { Chip } from "../../ui/Chip";
import { Button } from "../../ui/Button";
import { Spinner } from "../../ui/Spinner";
import { useToast } from "../../ui/Toast";
import type { ClientRow } from "../../lib/api";
import { sendReportNow, downloadClientReport } from "../../lib/api";

type Status = "draft" | "ready" | "sent" | "empty";

export interface QuarterCardProps {
  label: string;
  quarter: string;
  status: Status;
  arrayCount: number;
  lastDeliveredAt: string | null;
  mwhTotal: number;
  clients: ClientRow[];
  /** Start expanded (used for the most-recent complete quarter). */
  defaultExpanded?: boolean;
  onRefresh?: () => void;
}

const STATUS_CONFIG: Record<
  Status,
  { label: string; chipVariant: "emerald" | "wood" | "muted" }
> = {
  sent:  { label: "Sent",        chipVariant: "emerald" },
  ready: { label: "Ready",       chipVariant: "wood"    },
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

function clientDeliveryStatus(
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

/** Collapsed one-line header. */
function CollapsedRow({
  label,
  status,
  arrayCount,
  mwhTotal,
  lastDeliveredAt,
  onExpand,
}: {
  label: string;
  status: Status;
  arrayCount: number;
  mwhTotal: number;
  lastDeliveredAt: string | null;
  onExpand: () => void;
}) {
  const { label: statusLabel, chipVariant } = STATUS_CONFIG[status];

  const meta = [
    arrayCount > 0
      ? `${arrayCount} ${arrayCount === 1 ? "array" : "arrays"}`
      : null,
    mwhTotal > 0 ? `${mwhTotal.toFixed(2)} MWh` : null,
    mwhTotal > 0 ? `${Math.floor(mwhTotal)} RECs` : null,
    lastDeliveredAt ? `sent ${shortDate(lastDeliveredAt)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <button
      type="button"
      onClick={onExpand}
      className="flex w-full items-center justify-between gap-3 rounded-xl border border-cream-border bg-cream px-5 py-3.5 text-left shadow-sm transition-shadow hover:shadow-md"
    >
      <div className="min-w-0">
        <span className="text-sm font-semibold text-zinc-900">{label}</span>
        {meta && (
          <span className="ml-2 text-xs text-zinc-400">{meta}</span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Chip variant={chipVariant}>{statusLabel}</Chip>
        <span className="text-xs text-zinc-400" aria-hidden>
          ▾
        </span>
      </div>
    </button>
  );
}

/** Per-client row inside the expanded view. */
function ClientDeliveryRow({
  client,
  quarter,
  onRefresh,
}: {
  client: ClientRow;
  quarter: string;
  onRefresh?: () => void;
}) {
  const toast = useToast();
  const [sending, setSending] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const deliveryStatus = clientDeliveryStatus(client);

  async function handleResend() {
    setSending(true);
    try {
      const res = await sendReportNow([client.id]);
      const ok = res.results.find((r) => r.client_id === client.id);
      if (ok?.ok) {
        toast.success(`Re-sent to ${client.name}.`);
        onRefresh?.();
      } else {
        toast.error(ok?.reason ?? "Send failed.");
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Send failed");
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

  const statusChip = {
    sent:     <Chip variant="emerald">Sent</Chip>,
    bounced:  <Chip variant="red">Bounced</Chip>,
    no_email: <Chip variant="amber">No email</Chip>,
    pending:  <Chip variant="muted">Pending</Chip>,
  }[deliveryStatus];

  return (
    <div className="flex items-center gap-3 py-2">
      <div className="min-w-0 flex-1">
        <span className="text-sm text-zinc-800">{client.name}</span>
        {deliveryStatus === "bounced" && client.last_bounce_reason && (
          <span className="ml-2 text-xs text-red-500">
            {client.last_bounce_reason}
          </span>
        )}
        {deliveryStatus === "sent" && client.last_delivered_at && (
          <span className="ml-2 text-xs text-zinc-400">
            {shortDate(client.last_delivered_at)}
          </span>
        )}
        {deliveryStatus === "no_email" && (
          <span className="ml-2 text-xs text-amber-600">
            add contact email
          </span>
        )}
      </div>
      {statusChip}
      <Button
        variant="ghost"
        disabled={downloading}
        onClick={handleDownload}
        className="h-7 px-2 text-xs text-zinc-500"
      >
        {downloading ? <Spinner className="h-3 w-3" /> : ".xlsx"}
      </Button>
      <Button
        variant="ghost"
        disabled={sending || deliveryStatus === "no_email"}
        onClick={handleResend}
        className="h-7 px-2 text-xs text-zinc-500"
      >
        {sending ? <Spinner className="h-3 w-3" /> : "Re-send"}
      </Button>
    </div>
  );
}

export function QuarterCard({
  label,
  quarter,
  status,
  arrayCount,
  lastDeliveredAt,
  mwhTotal,
  clients,
  defaultExpanded = false,
  onRefresh,
}: QuarterCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  const collapse = useCallback(() => setExpanded(false), []);
  const expand   = useCallback(() => setExpanded(true),  []);

  if (!expanded) {
    return (
      <CollapsedRow
        label={label}
        status={status}
        arrayCount={arrayCount}
        mwhTotal={mwhTotal}
        lastDeliveredAt={lastDeliveredAt}
        onExpand={expand}
      />
    );
  }

  const { label: statusLabel, chipVariant } = STATUS_CONFIG[status];

  const metaParts = [
    arrayCount > 0 ? `${arrayCount} ${arrayCount === 1 ? "array" : "arrays"}` : null,
    mwhTotal > 0 ? `${mwhTotal.toFixed(2)} MWh` : null,
    mwhTotal > 0 ? `${Math.floor(mwhTotal)} RECs` : null,
    clients.length > 0
      ? `${clients.length} ${clients.length === 1 ? "client" : "clients"}`
      : null,
    lastDeliveredAt ? `sent ${shortDate(lastDeliveredAt)}` : null,
  ].filter(Boolean);

  return (
    <div className="rounded-xl border border-cream-border bg-cream shadow-sm">
      {/* Expanded header */}
      <button
        type="button"
        onClick={collapse}
        className="flex w-full items-start justify-between gap-3 px-5 py-4 text-left"
      >
        <div>
          <span className="text-base font-semibold text-zinc-900">{label}</span>
          <p className="mt-0.5 text-xs text-zinc-500">
            {metaParts.join(" · ")}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2 pt-0.5">
          <Chip variant={chipVariant}>{statusLabel}</Chip>
          <span className="text-xs text-zinc-400" aria-hidden>
            ▴
          </span>
        </div>
      </button>

      {/* Per-client delivery list */}
      {clients.length > 0 ? (
        <div className="border-t border-cream-border px-5 pb-4 pt-3">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Delivery status
          </p>
          <div className="divide-y divide-zinc-100">
            {clients.map((c) => (
              <ClientDeliveryRow
                key={c.id}
                client={c}
                quarter={quarter}
                onRefresh={onRefresh}
              />
            ))}
          </div>
        </div>
      ) : (
        <div className="border-t border-cream-border px-5 py-4">
          <p className="text-xs text-zinc-400">No clients in this quarter.</p>
        </div>
      )}
    </div>
  );
}
