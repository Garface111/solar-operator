import type { InputHTMLAttributes } from "react";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export function Input({ label, error, className = "", id, ...props }: InputProps) {
  return (
    <label className="block" htmlFor={id}>
      {label && (
        <span className="mb-1.5 block text-sm font-medium text-zinc-700">
          {label}
        </span>
      )}
      <input
        id={id}
        className={[
          "w-full rounded-xl border bg-white px-3.5 py-2.5 text-sm",
          "placeholder:text-zinc-400 transition-colors",
          "focus:outline-none focus:ring-2 focus:ring-offset-0",
          error
            ? "border-red-400 focus:ring-red-400"
            : "border-zinc-300 focus:ring-primary-500",
          className,
        ].join(" ")}
        {...props}
      />
      {error && <span className="mt-1 block text-xs text-red-600">{error}</span>}
    </label>
  );
}
