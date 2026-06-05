/** Human-readable "X ago", e.g. "2h ago", "3 days ago". */
export function timeAgo(past: Date): string {
  const diffMs = Date.now() - past.getTime();
  if (diffMs < 60_000) return "just now";
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(diffMs / 3_600_000);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(diffMs / 86_400_000);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** Human-readable relative time, e.g. "in 3h 12m" or "in 4 days". */
export function relativeTime(target: Date, overdueLabel = "soon"): string {
  const diffMs = target.getTime() - Date.now();
  if (diffMs <= 0) return overdueLabel;
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(diffMs / 3_600_000);
  if (hrs < 24) {
    const rem = Math.floor((diffMs % 3_600_000) / 60_000);
    return rem > 0 ? `in ${hrs}h ${rem}m` : `in ${hrs}h`;
  }
  const days = Math.ceil(diffMs / 86_400_000);
  return `in ${days} day${days === 1 ? "" : "s"}`;
}

/** Next scheduled send date based on report_frequency. */
export function nextReportDate(freq: string | null): Date {
  const now = new Date();
  const utcNow = now.getTime();
  if (freq === "monthly") {
    return now.getUTCMonth() === 11
      ? new Date(Date.UTC(now.getUTCFullYear() + 1, 0, 1, 9, 0, 0))
      : new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1, 9, 0, 0));
  }
  // default: quarterly (Jan 1/Apr 1/Jul 1/Oct 1 at 09:00 UTC)
  const year = now.getUTCFullYear();
  const candidates = [0, 3, 6, 9].map((m) => new Date(Date.UTC(year, m, 1, 9, 0, 0)));
  candidates.push(new Date(Date.UTC(year + 1, 0, 1, 9, 0, 0)));
  return candidates.find((d) => d.getTime() > utcNow)!;
}

export function fmtMoney(cents: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: currency.toUpperCase(),
      minimumFractionDigits: cents % 100 === 0 ? 0 : 2,
    }).format(cents / 100);
  } catch {
    return `$${(cents / 100).toFixed(2)}`;
  }
}
