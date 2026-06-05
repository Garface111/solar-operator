import { useState } from "react";
import { Chip } from "../../ui/Chip";
import { Button } from "../../ui/Button";
import { Spinner } from "../../ui/Spinner";
import type { ClientRow } from "../../lib/api";

type Status = "draft" | "ready" | "sent" | "empty";

export interface QuarterCardProps {
  label: string;
  status: Status;
  arrayCount: number;
  clientCount: number;
  /** ISO timestamp of the most-recent delivery, shown as relative time. */
  lastGeneratedAt: string | null;
  /** Total MWh generated in this quarter across all arrays, or null if unknown. */
  mwhTotal?: number | null;
  clients: ClientRow[];
  onDownload: (client: ClientRow) => Promise<void>;
  onRegenerate: () => Promise<void>;
}

const STATUS_CONFIG: Record<
  Status,
  { label: string; chipVariant: "emerald" | "wood" | "muted" }
> = {
  sent:  { label: "Sent",  chipVariant: "emerald" },
  ready: { label: "Ready", chipVariant: "wood"    },
  draft: { label: "Draft", chipVariant: "muted"   },
  empty: { label: "Empty", chipVariant: "muted"   },
};

function relativeTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  const min = Math.floor(ms / 60_000);
  if (min < 60) return `${min} minute${min === 1 ? "" : "s"} ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} hour${hr === 1 ? "" : "s"} ago`;
  const days = Math.floor(hr / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  const months = Math.floor(days / 30);
  return `${months} month${months === 1 ? "" : "s"} ago`;
}

export function QuarterCard({
  label,
  status,
  arrayCount,
  clientCount,
  lastGeneratedAt,
  mwhTotal,
  clients,
  onDownload,
  onRegenerate,
}: QuarterCardProps) {
  const [regenerating, setRegenerating] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [dlOpen, setDlOpen] = useState(false);

  const { label: statusLabel, chipVariant } = STATUS_CONFIG[status];
  const hasClients = clients.length > 0;

  async function handleRegenerate() {
    setRegenerating(true);
    try {
      await onRegenerate();
    } finally {
      setRegenerating(false);
    }
  }

  async function handleDownloadClient(client: ClientRow) {
    setDownloading(true);
    setDlOpen(false);
    try {
      await onDownload(client);
    } finally {
      setDownloading(false);
    }
  }

  async function handleDownloadAll() {
    setDownloading(true);
    setDlOpen(false);
    try {
      for (const client of clients) {
        await onDownload(client);
      }
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div
      aria-busy={regenerating}
      className="rounded-xl border border-cream-border bg-cream p-5 shadow-sm transition-all duration-150 hover:-translate-y-px hover:shadow-md"
    >
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-base font-semibold text-zinc-900">{label}</span>
        <Chip variant={chipVariant}>{statusLabel}</Chip>
      </div>

      {/* Stats */}
      <p className="mt-2 text-xs text-zinc-500">
        {arrayCount > 0
          ? `${arrayCount} array${arrayCount === 1 ? "" : "s"} · ${clientCount} client${clientCount === 1 ? "" : "s"}`
          : "No arrays configured"}
        {mwhTotal != null && mwhTotal > 0 && (
          <> · {mwhTotal.toFixed(1)} MWh</>
        )}
        {lastGeneratedAt && (
          <>
            {" · "}
            <span className="text-zinc-400">{relativeTime(lastGeneratedAt)}</span>
          </>
        )}
      </p>

      {/* Actions */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {/* Download — single client: direct; multiple clients: inline dropdown */}
        {clients.length <= 1 ? (
          <Button
            variant="secondary"
            disabled={downloading || !hasClients}
            onClick={() => clients[0] && handleDownloadClient(clients[0])}
            className="h-8 px-3 text-xs"
          >
            {downloading && <Spinner className="h-3.5 w-3.5" />}
            Download .xlsx
          </Button>
        ) : (
          <div className="relative">
            <Button
              variant="secondary"
              disabled={downloading}
              onClick={() => setDlOpen((o) => !o)}
              className="h-8 px-3 text-xs"
            >
              {downloading && <Spinner className="h-3.5 w-3.5" />}
              Download .xlsx
              <span
                className={`transition-transform duration-150 ${dlOpen ? "-rotate-180" : ""}`}
                aria-hidden
              >
                ▾
              </span>
            </Button>
            {dlOpen && (
              <>
                {/* Backdrop closes the dropdown on outside click */}
                <div
                  className="fixed inset-0 z-[9]"
                  onClick={() => setDlOpen(false)}
                />
                <div className="absolute left-0 top-full z-10 mt-1 min-w-[180px] overflow-hidden rounded-xl border border-cream-border bg-white shadow-md">
                  {clients.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      onClick={() => handleDownloadClient(c)}
                      className="block w-full px-4 py-2.5 text-left text-xs text-zinc-700 hover:bg-cream"
                    >
                      {c.name}
                    </button>
                  ))}
                  <div className="border-t border-zinc-100" />
                  <button
                    type="button"
                    onClick={handleDownloadAll}
                    className="block w-full px-4 py-2.5 text-left text-xs font-medium text-primary-700 hover:bg-cream"
                  >
                    Download all ({clients.length})
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* Regenerate — sends current reports to all clients */}
        <Button
          variant="ghost"
          disabled={regenerating}
          onClick={handleRegenerate}
          className="h-8 px-3 text-xs text-zinc-600"
        >
          {regenerating ? (
            <>
              <Spinner className="h-3.5 w-3.5" />
              Sending…
            </>
          ) : (
            "Regenerate"
          )}
        </Button>
      </div>
    </div>
  );
}
