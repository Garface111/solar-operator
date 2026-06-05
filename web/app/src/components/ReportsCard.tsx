import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Card } from "../ui/Card";
import { Chip } from "../ui/Chip";
import { type Account, type QuarterReport, getReports } from "../lib/api";

interface Props {
  account: Account;
  onAccountChange: (patch: Partial<Account>) => void;
}

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

/**
 * Compact reports status embed — suitable for the dashboard overview.
 * Shows the most recent sent quarter in a single line; links to /reports
 * for full history and send controls.
 */
export function ReportsCard({ account: _account, onAccountChange: _onAccountChange }: Props) {
  const [reports, setReports] = useState<QuarterReport[] | null>(null);

  useEffect(() => {
    getReports(2)
      .then(setReports)
      .catch(() => setReports([]));
  }, []);

  const lastSent = reports?.find((r) => r.status === "sent") ?? null;
  const anyPending = reports?.some((r) => r.status === "ready") ?? false;

  return (
    <Card>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-zinc-900">
            Automatic reports
          </h3>
          {lastSent ? (
            <p className="mt-0.5 text-xs text-zinc-500">
              {`Q${lastSent.quarter_num} ${lastSent.year}`}
              {lastSent.array_count > 0 && ` · ${lastSent.array_count} ${lastSent.array_count === 1 ? "array" : "arrays"}`}
              {lastSent.mwh_total > 0 && ` · ${lastSent.mwh_total.toFixed(2)} MWh`}
              {lastSent.last_delivered_at && ` · sent ${shortDate(lastSent.last_delivered_at)}`}
            </p>
          ) : reports !== null ? (
            <p className="mt-0.5 text-xs text-zinc-400">No reports sent yet</p>
          ) : (
            <div className="mt-1 h-3 w-48 animate-pulse rounded bg-zinc-100" />
          )}
        </div>
        {lastSent ? (
          <Chip variant="emerald">Sent</Chip>
        ) : anyPending ? (
          <Chip variant="emerald">Ready</Chip>
        ) : (
          <Chip variant="muted">Pending</Chip>
        )}
      </div>
      <div className="mt-3">
        <Link
          to="/reports"
          className="text-xs font-medium text-primary-600 hover:text-primary-700"
        >
          View reports &amp; send ↗
        </Link>
      </div>
    </Card>
  );
}
