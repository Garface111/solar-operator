import type { InputHTMLAttributes, ReactNode } from "react";

interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label?: ReactNode;
}

export function Checkbox({ label, className = "", id, ...props }: CheckboxProps) {
  return (
    <label
      htmlFor={id}
      className="flex cursor-pointer items-start gap-2.5 text-sm text-zinc-700"
    >
      <input
        id={id}
        type="checkbox"
        className={[
          "mt-0.5 h-4 w-4 rounded border-zinc-300 text-primary-500",
          "transition-colors duration-150 ease-in-out",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2",
          className,
        ].join(" ")}
        {...props}
      />
      {label && <span>{label}</span>}
    </label>
  );
}
