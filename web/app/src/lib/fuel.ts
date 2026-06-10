// ─── fuel types ────────────────────────────────────────────────────────────
// V2: an operator can report more than just solar. The backend Array model
// carries a `fuel_type` (default 'solar'), and this module is the single
// source of truth for how each fuel reads and looks in the UI.
//
// Design rules:
//  - Solar is the zero-friction default. Existing solar-only operators should
//    never see new chrome: badges only render for NON-solar arrays.
//  - Copy is plain and trust-building (kitchen-table voice), never enterprise.

export type FuelType = "solar" | "wind" | "hydro" | "digester" | "storage";

export const DEFAULT_FUEL: FuelType = "solar";

export interface FuelMeta {
  type: FuelType;
  /** Short label shown on pickers and badges. */
  label: string;
  /** A small glyph so a mixed-fuel list reads at a glance. */
  icon: string;
  /** One warm line explaining the option in the onboarding question. */
  hint: string;
  /** Tailwind classes for the small badge (bg + text + border). */
  badge: string;
  /** Tailwind classes for the picker pill when SELECTED. */
  pillOn: string;
}

// Order matters: solar first (the default), then the V2 additions.
export const FUELS: FuelMeta[] = [
  {
    type: "solar",
    label: "Solar",
    icon: "☀",
    hint: "Panels — the usual case.",
    badge: "bg-amber-50 text-amber-700 border-amber-200",
    pillOn: "border-amber-300 bg-amber-50 text-amber-800",
  },
  {
    type: "wind",
    label: "Wind",
    icon: "🌀",
    hint: "Turbines.",
    badge: "bg-sky-50 text-sky-700 border-sky-200",
    pillOn: "border-sky-300 bg-sky-50 text-sky-800",
  },
  {
    type: "hydro",
    label: "Hydro",
    icon: "💧",
    hint: "Running water.",
    badge: "bg-cyan-50 text-cyan-700 border-cyan-200",
    pillOn: "border-cyan-300 bg-cyan-50 text-cyan-800",
  },
  {
    type: "digester",
    label: "Digester",
    icon: "♻",
    hint: "Biogas from a farm or anaerobic digester.",
    badge: "bg-emerald-50 text-emerald-700 border-emerald-200",
    pillOn: "border-emerald-300 bg-emerald-50 text-emerald-800",
  },
  {
    type: "storage",
    label: "Storage",
    icon: "🔋",
    hint: "Batteries that charge and discharge.",
    badge: "bg-violet-50 text-violet-700 border-violet-200",
    pillOn: "border-violet-300 bg-violet-50 text-violet-800",
  },
];

const BY_TYPE: Record<FuelType, FuelMeta> = Object.fromEntries(
  FUELS.map((f) => [f.type, f]),
) as Record<FuelType, FuelMeta>;

/** Resolve a (possibly null/unknown) fuel value to its metadata, defaulting
 *  to solar so any legacy array without the field reads as solar. */
export function fuelMeta(type: FuelType | string | null | undefined): FuelMeta {
  if (type && type in BY_TYPE) return BY_TYPE[type as FuelType];
  return BY_TYPE[DEFAULT_FUEL];
}

export function isNonSolar(type: FuelType | string | null | undefined): boolean {
  return fuelMeta(type).type !== "solar";
}
