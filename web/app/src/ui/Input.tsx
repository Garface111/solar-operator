import { forwardRef, type InputHTMLAttributes } from "react";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, error, className = "", id, ...props },
  ref,
) {
  return (
    <label className="block" htmlFor={id}>
      {label && (
        <span className="mb-1.5 block text-sm font-medium text-zinc-700">
          {label}
        </span>
      )}
      <input
        ref={ref}
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={error && id ? `${id}-error` : undefined}
        className={[
          "w-full rounded-xl border bg-white px-3.5 py-2.5 text-sm",
          "placeholder:text-zinc-400 transition-colors duration-150 ease-in-out",
          "focus:outline-none focus:ring-2 focus:ring-offset-0 focus:border-transparent",
          error
            ? "border-red-400 focus:ring-red-400/50"
            : "border-zinc-300 focus:ring-primary-500/40",
          className,
        ].join(" ")}
        {...props}
      />
      {error && (
        <span
          id={id ? `${id}-error` : undefined}
          className="mt-1 block text-xs text-red-600"
        >
          {error}
        </span>
      )}
    </label>
  );
});
