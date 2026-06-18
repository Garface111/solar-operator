// Shared helpers for the multi-year billing trends view. Kept dependency-free
// (no charting lib in web/app) and on-theme (cream / primary-green / wood / zinc).

export const MONTH_INITIALS = [
  "J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D",
];

export const MONTH_ABBR = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

// Year line colors, newest year first so the most recent (most relevant) line
// reads boldest. 8 distinct hues so up to 8 years of history never share a
// color (Bruce/Norwich have 8). Latest year is the bold theme green; the rest
// spread across wood/teal/slate/blue tones that sit in the app's palette.
const YEAR_PALETTE = [
  "#10b981", // primary-600 green — latest year (bold)
  "#b56d2c", // wood-500 ochre
  "#0ea5e9", // sky-500 blue
  "#d4914a", // wood-400 tan
  "#0d9488", // teal-600
  "#71717a", // zinc-500 slate
  "#9333ea", // violet-600
  "#a16207", // amber-700 deep gold
];

/** Deterministic color for a year given the full (any-order) set of years.
 *  The latest year always maps to the boldest palette entry. */
export function yearColor(year: number, years: number[]): string {
  const desc = [...years].sort((a, b) => b - a); // newest → oldest
  const idx = desc.indexOf(year);
  const i = idx < 0 ? years.length : idx;
  return YEAR_PALETTE[i % YEAR_PALETTE.length];
}

/** Whole-number kWh with thousands separators, e.g. 24890 → "24,890". */
export function formatKwh(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

/** Compact kWh for axis ticks: 1820 → "1.8k", 980 → "980". */
export function formatKwhCompact(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1000) {
    const k = n / 1000;
    return `${Number.isInteger(k) ? k : k.toFixed(1)}k`;
  }
  return String(Math.round(n));
}

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return USD.format(n);
}

/** Signed delta percent, e.g. 4.8 → "+4.8%", -3.21 → "-3.2%". */
export function formatDeltaPct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "—";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(1)}%`;
}

export type DeltaTone = "up" | "down" | "flat" | "none";

export function deltaTone(pct: number | null | undefined): DeltaTone {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return "none";
  if (pct > 0) return "up";
  if (pct < 0) return "down";
  return "flat";
}
