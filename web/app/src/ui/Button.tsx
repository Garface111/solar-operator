import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-primary-500 text-white hover:bg-primary-600 active:bg-primary-700 focus-visible:ring-primary-500/40",
  secondary:
    "bg-white text-zinc-900 border border-zinc-300 hover:bg-zinc-50 active:bg-zinc-100 focus-visible:ring-primary-500/40",
  ghost:
    "bg-transparent text-zinc-700 hover:bg-zinc-100 active:bg-zinc-200 focus-visible:ring-primary-500/40",
  danger:
    "bg-white text-red-600 border border-red-300 hover:bg-red-50 active:bg-red-100 focus-visible:ring-red-500/40",
};

export function Button({
  variant = "primary",
  className = "",
  ...props
}: ButtonProps) {
  return (
    <button
      className={[
        "inline-flex items-center justify-center gap-2 rounded-xl px-5 py-2.5",
        "text-sm font-medium transition-colors duration-150 ease-in-out",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2",
        "disabled:opacity-50 disabled:cursor-not-allowed",
        VARIANTS[variant],
        className,
      ].join(" ")}
      {...props}
    />
  );
}
