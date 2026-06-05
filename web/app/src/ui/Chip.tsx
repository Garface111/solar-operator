import type { HTMLAttributes } from "react";

type ChipVariant = "default" | "emerald" | "amber" | "red" | "wood" | "muted";

interface ChipProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: ChipVariant;
}

const VARIANTS: Record<ChipVariant, string> = {
  default: "bg-zinc-100 text-zinc-600 border border-zinc-200",
  emerald: "bg-primary-50 text-primary-700 border border-primary-200",
  amber:   "bg-amber-50 text-amber-700 border border-amber-200",
  red:     "bg-red-50 text-red-700 border border-red-200",
  wood:    "bg-wood-100 text-wood-600 border border-wood-border",
  muted:   "bg-zinc-100 text-zinc-500",
};

export function Chip({ variant = "default", className = "", ...props }: ChipProps) {
  return (
    <span
      className={[
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-medium",
        VARIANTS[variant],
        className,
      ].join(" ")}
      {...props}
    />
  );
}
