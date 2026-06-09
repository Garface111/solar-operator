// Volume/graduated per-array pricing — TS mirror of api/pricing.py.
// MUST stay in sync with the backend table and the live Stripe graduated price.
// Graduated: each band's unit price applies only to the arrays within that band.
//
//   arrays 1–50     $15.00  (0% off — full)
//   arrays 51–100   $13.50  (10% off)
//   arrays 101–150  $12.00  (20% off)
//   arrays 151+     $10.50  (30% off — cap)

type Tier = { upTo: number | null; unitCents: number };

export const TIERS: Tier[] = [
  { upTo: 50, unitCents: 1500 },
  { upTo: 100, unitCents: 1350 },
  { upTo: 150, unitCents: 1200 },
  { upTo: null, unitCents: 1050 },
];

export const FULL_UNIT_CENTS = TIERS[0].unitCents; // $15.00

/** Total monthly cents for a given array count under graduated tiers. */
export function monthlyCents(count: number): number {
  if (count <= 0) return 0;
  let remaining = count;
  let prevBound = 0;
  let total = 0;
  for (const { upTo, unitCents } of TIERS) {
    if (upTo === null) {
      total += remaining * unitCents;
      break;
    }
    const take = Math.min(remaining, upTo - prevBound);
    total += take * unitCents;
    remaining -= take;
    prevBound = upTo;
    if (remaining <= 0) break;
  }
  return total;
}

/** Whole-dollar monthly total (Stripe prices are whole-dollar units here). */
export function monthlyDollars(count: number): number {
  return Math.round(monthlyCents(count) / 100);
}

/** True once the count earns any volume discount (past the first tier). */
export function hasVolumeDiscount(count: number): boolean {
  return count > (TIERS[0].upTo ?? Infinity);
}

/** What the same count would cost with no volume discount (flat $15). */
export function flatMonthlyCents(count: number): number {
  return Math.max(0, count) * FULL_UNIT_CENTS;
}

/** Monthly cents saved by volume pricing vs the flat $15 rate. */
export function savingsCents(count: number): number {
  return flatMonthlyCents(count) - monthlyCents(count);
}

/** Average per-array cents actually paid (total / count), rounded. */
export function blendedUnitCents(count: number): number {
  if (count <= 0) return FULL_UNIT_CENTS;
  return Math.round(monthlyCents(count) / count);
}

/** Discount percentage off the flat rate, rounded (0 below first tier). */
export function discountPct(count: number): number {
  const flat = flatMonthlyCents(count);
  if (flat <= 0) return 0;
  return Math.round((savingsCents(count) / flat) * 100);
}

export interface BandRow {
  /** Human label for the band, e.g. "1–50" or "151+". */
  label: string;
  /** Per-array price in this band, cents. */
  unitCents: number;
  /** Percent off the full $15 rate for this band (0, 10, 20, 30). */
  offPct: number;
  /** How many of THIS operator's arrays fall in the band (0 if none yet). */
  filled: number;
  /** Total arrays the band can hold (Infinity for the final band). */
  capacity: number;
  /** filled * unitCents — what this band contributes to the bill. */
  subtotalCents: number;
  /** True if the operator's count reaches into this band at all. */
  active: boolean;
}

/**
 * Per-band breakdown of how `count` arrays fill the graduated tiers.
 * Always returns all four bands so the UI can render the full ladder and
 * light up the active ones as the count grows.
 */
export function tierBreakdown(count: number): BandRow[] {
  const n = Math.max(0, count);
  let prevBound = 0;
  const rows: BandRow[] = [];
  for (const { upTo, unitCents } of TIERS) {
    const capacity = upTo === null ? Infinity : upTo - prevBound;
    const lower = prevBound + 1;
    const label = upTo === null ? `${lower}+` : `${lower}–${upTo}`;
    const remainingAtBand = Math.max(0, n - prevBound);
    const filled = Math.min(remainingAtBand, capacity);
    const offPct = Math.round((1 - unitCents / FULL_UNIT_CENTS) * 100);
    rows.push({
      label,
      unitCents,
      offPct,
      filled,
      capacity,
      subtotalCents: filled * unitCents,
      active: filled > 0,
    });
    if (upTo !== null) prevBound = upTo;
  }
  return rows;
}
