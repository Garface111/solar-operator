/**
 * Cross-surface fleet refresh for the NEPOOL dashboard.
 *
 * The Clients tab keeps Sandbox + Table mounted side-by-side; Account counts
 * and billing live in other tabs. A sandbox delete used to update only the
 * canvas nodes — Table / Master Account / billing stayed stale until a full
 * page reload. Broadcast once from any mutation path so every surface refetches.
 *
 * Fires three event names for back-compat with existing listeners:
 *   so:fleet-changed   — new unified signal (preferred)
 *   so:sandbox:mutated — ClientsSection table refresh
 *   so:arrays-changed  — billing, ArrayList, NEPOOL banner, etc.
 *
 * `source` lets the canvas ignore its own broadcasts (avoids reload loops).
 */
export type FleetChangeSource =
  | "canvas" // full canvas reload just finished
  | "sandbox-delete"
  | "sandbox-mutate"
  | "table-delete"
  | "array-delete"
  | "array-edit"
  | "account-refresh"
  | string;

export const FLEET_CHANGED = "so:fleet-changed";
export const SANDBOX_MUTATED = "so:sandbox:mutated";
export const ARRAYS_CHANGED = "so:arrays-changed";

export function notifyFleetChanged(source: FleetChangeSource = "unknown"): void {
  try {
    const detail = { source };
    window.dispatchEvent(new CustomEvent(FLEET_CHANGED, { detail }));
    window.dispatchEvent(new CustomEvent(SANDBOX_MUTATED, { detail }));
    window.dispatchEvent(new CustomEvent(ARRAYS_CHANGED, { detail }));
  } catch {
    /* SSR / private mode — ignore */
  }
}

/** Read source from a CustomEvent detail (best-effort). */
export function fleetChangeSource(e: Event): string {
  const d = (e as CustomEvent<{ source?: string }>).detail;
  return (d && typeof d.source === "string" && d.source) || "unknown";
}
