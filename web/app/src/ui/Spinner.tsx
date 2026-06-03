interface SpinnerProps {
  /** Tailwind size classes, defaults to a 1rem inline spinner. */
  className?: string;
  /** Accessible label for screen readers; visually hidden. */
  label?: string;
}

/** Minimal inline loading spinner — inherits text color via `currentColor`. */
export function Spinner({ className = "h-4 w-4", label = "Loading" }: SpinnerProps) {
  return (
    <svg
      className={`animate-spin ${className}`}
      viewBox="0 0 24 24"
      fill="none"
      role="status"
      aria-label={label}
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 0 1 8-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  );
}
