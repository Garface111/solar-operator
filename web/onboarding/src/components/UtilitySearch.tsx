import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchProviders,
  type Provider,
  type ProviderStatus,
} from "../lib/onboarding";

/* ─── "Is my utility supported?" search ──────────────────────────────────────
   A prospect on the home page types their utility (or state) and instantly
   sees whether automated capture is live today, on the roadmap, or
   manual-only. Honest by construction — the three states map 1:1 to the
   backend scrape_status, so we never claim automation that isn't wired.

   Data source: GET /v1/providers (public). Fetched once, filtered client-side
   (the catalog is ~1.4k tiny rows). Ford Jun 8'26 voice: artifact/answer first,
   no overclaiming, click = tax (results appear as you type — no submit button).
   ─────────────────────────────────────────────────────────────────────────── */

const STATUS_META: Record<
  ProviderStatus,
  { badge: string; pillClass: string; blurb: string; dot: string }
> = {
  live: {
    badge: "Supported",
    pillClass: "bg-primary-50 text-primary-700 ring-1 ring-primary-200",
    blurb: "Automated capture — bills pulled for you every quarter.",
    dot: "bg-primary-500",
  },
  "in-progress": {
    badge: "On the roadmap",
    pillClass: "bg-amber-50 text-amber-700 ring-1 ring-amber-200",
    blurb: "Not automated yet — you can upload bills manually in the meantime.",
    dot: "bg-amber-500",
  },
  manual: {
    badge: "Manual upload",
    pillClass: "bg-zinc-100 text-zinc-600 ring-1 ring-zinc-200",
    blurb: "No online portal — email us your bills and we handle the rest.",
    dot: "bg-zinc-400",
  },
};

const MAX_RESULTS = 8;

function rank(p: Provider): number {
  // live first, then in-progress, then manual — so the best answer leads.
  return p.scrape_status === "live" ? 0 : p.scrape_status === "in-progress" ? 1 : 2;
}

export default function UtilitySearch() {
  const [all, setAll] = useState<Provider[] | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [raw, setRaw] = useState("");
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  // Load the catalog once.
  useEffect(() => {
    let alive = true;
    fetchProviders()
      .then((ps) => alive && setAll(ps))
      .catch((e) => alive && setLoadErr(e?.message || "Couldn't load the list."));
    return () => {
      alive = false;
    };
  }, []);

  // Debounce the query so we don't re-filter on every keystroke.
  useEffect(() => {
    const t = setTimeout(() => setQuery(raw.trim().toLowerCase()), 140);
    return () => clearTimeout(t);
  }, [raw]);

  const liveCount = useMemo(
    () => (all ? all.filter((p) => p.scrape_status === "live").length : 0),
    [all],
  );

  const results = useMemo(() => {
    if (!all || query.length < 2) return [];
    const q = query;
    const matches = all.filter(
      (p) =>
        p.label.toLowerCase().includes(q) ||
        p.state.toLowerCase() === q ||
        p.code.toLowerCase().includes(q),
    );
    matches.sort((a, b) => rank(a) - rank(b) || a.label.localeCompare(b.label));
    return matches;
  }, [all, query]);

  const showEmpty = query.length >= 2 && all && results.length === 0;

  return (
    <section
      aria-labelledby="utility-search-heading"
      className="mx-auto mt-10 max-w-2xl rounded-3xl border border-zinc-200 bg-white p-6 shadow-[0_20px_60px_-30px_rgba(0,0,0,0.18)] sm:p-8"
    >
      <div className="text-center">
        <p className="text-xs font-semibold uppercase tracking-wider text-primary-700">
          Check coverage
        </p>
        <h2
          id="utility-search-heading"
          className="mt-1 text-2xl font-semibold tracking-tight text-zinc-900"
        >
          Is your utility supported?
        </h2>
        <p className="mt-2 text-sm text-zinc-500">
          {liveCount > 0
            ? `${liveCount} utilities are automated today — search yours.`
            : "Search your electric utility to see how it connects."}
        </p>
      </div>

      <div className="relative mt-5">
        <span
          aria-hidden
          className="pointer-events-none absolute left-4 top-1/2 -translate-y-1/2 text-zinc-400"
        >
          🔍
        </span>
        <input
          ref={inputRef}
          type="text"
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          placeholder="e.g. Green Mountain Power, or a state like VT"
          aria-label="Search for your utility"
          autoComplete="off"
          className="w-full rounded-xl border border-zinc-300 bg-white py-3 pl-11 pr-4 text-sm placeholder:text-zinc-400 transition-colors duration-150 focus:border-transparent focus:outline-none focus:ring-2 focus:ring-primary-500/40"
        />
      </div>

      {/* States: error, prompt, results, empty */}
      {loadErr && (
        <p className="mt-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
          {loadErr} You can still start setup — we'll confirm your utility there.
        </p>
      )}

      {!loadErr && !all && (
        <p className="mt-4 text-center text-sm text-zinc-400">Loading the list…</p>
      )}

      {!loadErr && all && query.length < 2 && (
        <p className="mt-4 text-center text-xs text-zinc-400">
          Type at least two letters. Co-ops, municipals, and Green Mountain Power
          are automated; investor-owned portals are rolling out.
        </p>
      )}

      {results.length > 0 && (
        <ul className="mt-4 space-y-2">
          {results.slice(0, MAX_RESULTS).map((p) => {
            const meta = STATUS_META[p.scrape_status];
            return (
              <li
                key={p.code}
                className="flex items-start justify-between gap-3 rounded-xl border border-zinc-100 bg-zinc-50/60 px-4 py-3"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span
                      aria-hidden
                      className={`h-2 w-2 shrink-0 rounded-full ${meta.dot}`}
                    />
                    <p className="truncate text-sm font-semibold text-zinc-900">
                      {p.label}
                    </p>
                    {p.state && (
                      <span className="shrink-0 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
                        {p.state}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-xs text-zinc-500">{meta.blurb}</p>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2.5 py-1 text-[11px] font-semibold ${meta.pillClass}`}
                >
                  {meta.badge}
                </span>
              </li>
            );
          })}
          {results.length > MAX_RESULTS && (
            <li className="px-1 pt-1 text-center text-xs text-zinc-400">
              +{results.length - MAX_RESULTS} more — keep typing to narrow it down.
            </li>
          )}
        </ul>
      )}

      {showEmpty && (
        <div className="mt-4 rounded-xl border border-zinc-100 bg-zinc-50/60 px-4 py-4 text-center">
          <p className="text-sm font-medium text-zinc-700">
            We don't list that one yet.
          </p>
          <p className="mt-1 text-xs text-zinc-500">
            We can still onboard you with manual bill uploads while we add it.
            Start setup and tell us your utility — coverage is expanding every
            week.
          </p>
        </div>
      )}
    </section>
  );
}
