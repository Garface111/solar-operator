interface SectionTitleProps {
  title: string;
  subtitle?: string;
  /** Numeric badge shown in muted text after the title (e.g. item count). */
  count?: number;
}

export function SectionTitle({ title, subtitle, count }: SectionTitleProps) {
  return (
    <div>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
        {title}
        {count !== undefined && (
          <span className="ml-2 text-sm font-normal text-zinc-400">{count}</span>
        )}
      </h2>
      {subtitle && (
        <p className="mt-0.5 text-sm text-zinc-500">{subtitle}</p>
      )}
    </div>
  );
}
