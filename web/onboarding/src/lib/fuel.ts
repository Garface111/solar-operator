// ─── fuel types (onboarding) ───────────────────────────────────────────────
// Mirror of web/app/src/lib/fuel.ts, kept self-contained so the onboarding SPA
// has no cross-package import. V2: an operator may report wind/hydro/digester/
// storage as well as solar. Solar is the zero-friction default — picking it
// changes nothing about the existing flow.

export type FuelType = "solar" | "wind" | "hydro" | "digester" | "storage";

export const DEFAULT_FUEL: FuelType = "solar";

export interface FuelMeta {
  type: FuelType;
  label: string;
  icon: string;
  /** Warm one-liner for the onboarding question. */
  hint: string;
  /** Tailwind classes for the picker pill when SELECTED. */
  pillOn: string;
}

export const FUELS: FuelMeta[] = [
  {
    type: "solar",
    label: "Solar",
    icon: "☀",
    hint: "Panels — the usual case.",
    pillOn: "border-amber-300 bg-amber-50 text-amber-800",
  },
  {
    type: "wind",
    label: "Wind",
    icon: "🌀",
    hint: "Turbines.",
    pillOn: "border-sky-300 bg-sky-50 text-sky-800",
  },
  {
    type: "hydro",
    label: "Hydro",
    icon: "💧",
    hint: "Running water.",
    pillOn: "border-cyan-300 bg-cyan-50 text-cyan-800",
  },
  {
    type: "digester",
    label: "Digester",
    icon: "♻",
    hint: "Biogas from a farm or anaerobic digester.",
    pillOn: "border-emerald-300 bg-emerald-50 text-emerald-800",
  },
  {
    type: "storage",
    label: "Storage",
    icon: "🔋",
    hint: "Batteries that charge and discharge.",
    pillOn: "border-violet-300 bg-violet-50 text-violet-800",
  },
];

export function fuelMeta(type: FuelType | string | null | undefined): FuelMeta {
  return FUELS.find((f) => f.type === type) ?? FUELS[0];
}
