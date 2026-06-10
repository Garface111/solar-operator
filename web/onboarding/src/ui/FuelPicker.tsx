import { FUELS, type FuelType } from "../lib/fuel";

// A small repeat-clickable row of fuel pills for onboarding. Solar is the
// default, so a solar-only operator just leaves it be. Used both for the
// per-client "what kind of generation" question and per-array overrides.

export function FuelPicker({
  value,
  onChange,
  label,
  size = "md",
}: {
  value: FuelType;
  onChange: (f: FuelType) => void;
  label?: string;
  size?: "sm" | "md";
}) {
  const pad = size === "sm" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs";
  return (
    <div>
      {label && (
        <span className="mb-1.5 block text-sm font-medium text-zinc-700">{label}</span>
      )}
      <div role="radiogroup" aria-label={label ?? "Generation type"} className="flex flex-wrap gap-1.5">
        {FUELS.map((f) => {
          const selected = value === f.type;
          return (
            <button
              key={f.type}
              type="button"
              role="radio"
              aria-checked={selected}
              onClick={() => onChange(f.type)}
              title={f.hint}
              className={[
                "inline-flex items-center gap-1 rounded-full border font-medium transition-colors duration-150 ease-in-out focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40",
                pad,
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
