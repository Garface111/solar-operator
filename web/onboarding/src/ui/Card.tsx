import type { HTMLAttributes } from "react";

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Active step cards get a stronger shadow per the design spec. */
  active?: boolean;
}

export function Card({ active = false, className = "", ...props }: CardProps) {
  return (
    <div
      className={[
        "bg-white border border-zinc-200 rounded-xl p-8",
        active ? "shadow-lg" : "shadow-sm",
        className,
      ].join(" ")}
      {...props}
    />
  );
}
