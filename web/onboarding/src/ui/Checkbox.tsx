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
          "focus:ring-2 focus:ring-primary-500 focus:ring-offset-0",
          className,
        ].join(" ")}
        {...props}
      />
      {label && <span>{label}</span>}
    </label>
  );
}
