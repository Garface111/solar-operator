/** One-glance "did this quarter's reports go out?" indicator. */

export type ShipStatus = "all_shipped" | "partial" | "not_yet" | "in_progress";

interface Props {
  status: ShipStatus;
  /** e.g. "Q1 2026" — shown as a muted prefix before the status label */
  quarter?: string;
}

const CONFIGS: Record<ShipStatus, { label: string; cls: string }> = {
  all_shipped:  { label: "ALL SHIPPED ✓",    cls: "bg-primary-50 text-primary-700 border border-primary-200" },
  partial:      { label: "PARTIALLY SHIPPED", cls: "bg-amber-50 text-amber-700 border border-amber-200" },
  not_yet:      { label: "NOT YET",           cls: "bg-zinc-100 text-zinc-600 border border-zinc-200" },
  in_progress:  { label: "IN PROGRESS",       cls: "bg-wood-100 text-wood-600 border border-wood-border" },
};

export function StatusPill({ status, quarter }: Props) {
  const { label, cls } = CONFIGS[status];
  return (
    <span
      className={[
        "inline-flex items-center gap-1.5 rounded-full px-3 py-1",
        "text-xs font-semibold tracking-wide",
        cls,
      ].join(" ")}
    >
      {quarter && (
        <>
          <span className="font-normal opacity-60">{quarter}</span>
          <span className="opacity-30">·</span>
        </>
      )}
      {label}
    </span>
  );
}
