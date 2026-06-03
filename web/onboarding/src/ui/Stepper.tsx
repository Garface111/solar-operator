interface StepperProps {
  /** Step labels, rendered as a numbered pill list across the top. */
  steps: string[];
  /** Zero-based index of the active step. */
  current: number;
}

export function Stepper({ steps, current }: StepperProps) {
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {steps.map((label, i) => {
        const isActive = i === current;
        const isDonePrior = i < current;
        return (
          <li key={label} className="flex items-center gap-2">
            <div
              className={[
                "flex items-center gap-2 rounded-full px-3 py-1.5 text-sm font-medium transition-colors",
                isActive
                  ? "bg-primary-500 text-white"
                  : isDonePrior
                    ? "bg-primary-100 text-primary-700"
                    : "bg-zinc-100 text-zinc-500",
              ].join(" ")}
            >
              <span
                className={[
                  "flex h-5 w-5 items-center justify-center rounded-full text-xs",
                  isActive
                    ? "bg-white/20 text-white"
                    : isDonePrior
                      ? "bg-primary-200 text-primary-800"
                      : "bg-zinc-200 text-zinc-600",
                ].join(" ")}
              >
                {i + 1}
              </span>
              <span className="hidden sm:inline">{label}</span>
            </div>
            {i < steps.length - 1 && (
              <span aria-hidden className="h-px w-4 bg-zinc-200" />
            )}
          </li>
        );
      })}
    </ol>
  );
}
