import { FUELS, fuelMeta, type FuelType } from "../lib/fuel";

// ─── FuelBadge ─────────────────────────────────────────────────────────────
// A small fuel marker shown on an array. Solar arrays render NOTHING by
// default (showSolar=false) so the solar-only experience is unchanged — a
// badge only appears when an operator actually mixes in wind/hydro/etc.

export function FuelBadge({
  fuel,
  showSolar = false,
  className = "",
}: {
  fuel: FuelType | string | null | undefined;
  showSolar?: boolean;
  className?: string;
}) {
  const meta = fuelMeta(fuel);
  if (meta.type === "solar" && !showSolar) return null;
  return (
    <span
      title={meta.label}
      aria-label={meta.label}
      className={[
        "inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold",
        meta.badge,
        className,
      ].join(" ")}
    >
      <span aria-hidden>{meta.icon}</span>
      {meta.label}
    </span>
  );
}

// ─── FuelPicker ────────────────────────────────────────────────────────────
// A repeat-clickable row of pills (Ford prefers a quick picker over a modal /
// dropdown form). Click a pill, it selects — keep clicking as you add arrays.
// Solar is pre-selected so the common case is zero extra clicks.

export function FuelPicker({
  value,
  onChange,
  label = "Generation type",
  className = "",
}: {
  value: FuelType;
  onChange: (f: FuelType) => void;
  label?: string;
  className?: string;
}) {
  return (
    <div className={className}>
      {label && (
        <span className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-zinc-400">
          {label}
        </span>
      )}
      <div role="radiogroup" aria-label={label} className="flex flex-wrap gap-1.5">
        {FUELS.map((f) => {
          const selected = value === f.type;
          return (
            <button
              key={f.type}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => onChange(f.type)}
              className={[
                "inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                selected
                  ? f.pillOn
                  : "border-zinc-200 bg-white text-zinc-500 hover:border-zinc-300 hover:text-zinc-700",
              ].join(" ")}
            >
              <span aria-hidden>{f.icon}</span>
              {f.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}
