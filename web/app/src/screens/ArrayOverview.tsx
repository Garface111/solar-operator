import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  arrayOwnersOverview,
  connectSolarEdge,
  UnauthorizedError,
} from "../lib/api";
import type {
  ArrayHealthStatus,
  ArrayOwnerArray,
  ArrayOwnersOverview,
} from "../lib/arrayOwners";

const POLL_MS = 60_000;

// ─── animation ──────────────────────────────────────────────────────────────

/** Tiny ease-out count-up. Animates the displayed number toward `value`
 *  whenever it changes — no animation libraries. Starts from 0 on first mount
 *  for a satisfying intro, then animates between subsequent values. */
function useCountUp(value: number, durationMs = 900): number {
  const [display, setDisplay] = useState(0);
  const rafRef = useRef<number | undefined>(undefined);
  const displayRef = useRef(0);
  displayRef.current = display;

  useEffect(() => {
    const from = displayRef.current;
    const to = value;
    if (from === to) return;
    let start: number | null = null;

    function tick(ts: number) {
      if (start === null) start = ts;
      const t = Math.min(1, (ts - start) / durationMs);
      const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic
      setDisplay(from + (to - from) * eased);
      if (t < 1) rafRef.current = requestAnimationFrame(tick);
    }
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== undefined) cancelAnimationFrame(rafRef.current);
    };
  }, [value, durationMs]);

  return display;
}

interface AnimatedNumberProps {
  value: number;
  format: (n: number) => string;
  className?: string;
}

/** Renders a single number that count-ups smoothly when `value` changes. */
function AnimatedNumber({ value, format, className }: AnimatedNumberProps) {
  const shown = useCountUp(value);
  return (
    <span className={className} aria-label={format(value)}>
      {format(shown)}
    </span>
  );
}

// ─── formatting ───────────────────────────────────────────────────────────

/** Watts → "830 W" / "4.83 kW" with an auto unit switch at 1 kW. */
function formatPower(watts: number): string {
  if (watts >= 1000) return `${(watts / 1000).toFixed(2)} kW`;
  return `${Math.round(watts)} W`;
}

function formatKwh(kwh: number): string {
  const digits = kwh >= 1000 ? 0 : 1;
  return `${kwh.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} kWh`;
}

function formatUsd(usd: number): string {
  return usd.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

/** Compact $ for dense card rows (e.g. "$1,024.31"). */
function formatUsdShort(usd: number): string {
  return formatUsd(usd);
}

// ─── live power gauge ───────────────────────────────────────────────────────

interface PowerGaugeProps {
  /** Current output in watts. */
  watts: number;
  /** Scale reference — the arc fills to watts/peak. */
  peakWatts: number;
  live: boolean;
}

/**
 * Semicircular SVG arc gauge — no chart libraries. The arc fills proportional
 * to current output against `peakWatts` (the largest output we've seen for this
 * array this session, since the contract doesn't expose nameplate capacity).
 */
function PowerGauge({ watts, peakWatts, live }: PowerGaugeProps) {
  const frac = peakWatts > 0 ? Math.min(1, Math.max(0, watts / peakWatts)) : 0;
  const animFrac = useCountUp(frac);
  // pathLength is normalised to 100, so offset is just (1 - fraction) * 100.
  const offset = 100 * (1 - animFrac);

  return (
    <div className="relative flex flex-col items-center">
      <svg viewBox="0 0 100 56" className="w-full max-w-[180px]" aria-hidden>
        {/* track */}
        <path
          d="M 8 50 A 42 42 0 0 1 92 50"
          fill="none"
          stroke="#e8e2d9"
          strokeWidth={8}
          strokeLinecap="round"
          pathLength={100}
        />
        {/* fill */}
        <path
          d="M 8 50 A 42 42 0 0 1 92 50"
          fill="none"
          stroke={live ? "#34d399" : "#d4d4d8"}
          strokeWidth={8}
          strokeLinecap="round"
          pathLength={100}
          strokeDasharray={100}
          strokeDashoffset={offset}
          style={{ transition: "stroke 300ms ease" }}
        />
      </svg>
      <div className="-mt-7 flex flex-col items-center">
        <AnimatedNumber
          value={watts}
          format={formatPower}
          className="text-xl font-semibold tabular-nums text-zinc-900"
        />
        <span className="text-[10px] uppercase tracking-wide text-zinc-400">
          {live ? "live output" : "no live data"}
        </span>
      </div>
    </div>
  );
}

// ─── health pill ────────────────────────────────────────────────────────────

const HEALTH_STYLES: Record<
  ArrayHealthStatus,
  { dot: string; text: string; bg: string; label: string }
> = {
  ok: {
    dot: "bg-primary-500",
    text: "text-primary-700",
    bg: "bg-primary-50",
    label: "Healthy",
  },
  stale: {
    dot: "bg-wood-400",
    text: "text-wood-600",
    bg: "bg-wood-50",
    label: "Stale data",
  },
  offline: {
    dot: "bg-red-500",
    text: "text-red-700",
    bg: "bg-red-50",
    label: "Offline",
  },
  no_source: {
    dot: "bg-zinc-400",
    text: "text-zinc-500",
    bg: "bg-zinc-100",
    label: "No source",
  },
};

function HealthPill({ status, message }: { status: ArrayHealthStatus; message: string }) {
  const s = HEALTH_STYLES[status];
  return (
    <span
      title={message}
      className={[
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        s.bg,
        s.text,
      ].join(" ")}
    >
      <span className={["h-1.5 w-1.5 rounded-full", s.dot].join(" ")} />
      {s.label}
    </span>
  );
}

// ─── connect inverter modal ──────────────────────────────────────────────────

interface ConnectModalProps {
  array: ArrayOwnerArray | null;
  onClose: () => void;
  onConnected: () => void;
}

function ConnectInverterModal({ array, onClose, onConnected }: ConnectModalProps) {
  const toast = useToast();
  const [apiKey, setApiKey] = useState("");
  const [siteId, setSiteId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the form whenever a different array opens the modal.
  useEffect(() => {
    setApiKey("");
    setSiteId("");
    setError(null);
    setSaving(false);
  }, [array?.array_id]);

  const siteIdNum = Number(siteId.trim());
  const valid =
    apiKey.trim().length > 0 &&
    siteId.trim().length > 0 &&
    Number.isInteger(siteIdNum) &&
    siteIdNum > 0;

  async function handleConnect() {
    if (!array || !valid || saving) return;
    setSaving(true);
    setError(null);
    try {
      const res = await connectSolarEdge(array.array_id, apiKey.trim(), siteIdNum);
      toast.success(`Connected ${res.site_name}`);
      onConnected();
      onClose();
    } catch (err) {
      if (err instanceof UnauthorizedError) return; // handled globally
      // 400s arrive as a thrown Error carrying the server's detail — show it
      // inline rather than as a transient toast so the operator can fix it.
      setError(err instanceof Error ? err.message : "Couldn't connect the inverter");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={array !== null}
      onClose={() => {
        if (!saving) onClose();
      }}
      title={array ? `Connect inverter — ${array.name}` : "Connect inverter"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={handleConnect} disabled={!valid || saving}>
            {saving ? (
              <>
                <Spinner />
                Connecting…
              </>
            ) : (
              "Connect"
            )}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-sm text-zinc-500">
          Paste your SolarEdge monitoring API key and the site ID for this
          array. We validate the key with SolarEdge before saving.
        </p>
        <Input
          id="se-api-key"
          label="SolarEdge API key"
          autoFocus
          placeholder="e.g. ABCD1234EFGH5678"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <Input
          id="se-site-id"
          label="Site ID"
          inputMode="numeric"
          placeholder="e.g. 1234567"
          value={siteId}
          onChange={(e) => setSiteId(e.target.value.replace(/[^0-9]/g, ""))}
          error={error ?? undefined}
        />
        <p className="text-xs text-zinc-400">
          Find both in the SolarEdge monitoring portal under Admin → Site
          Access → API Access.
        </p>
      </div>
    </Modal>
  );
}

// ─── totals band ──────────────────────────────────────────────────────────

function TotalStat({
  label,
  value,
  format,
}: {
  label: string;
  value: number;
  format: (n: number) => string;
}) {
  return (
    <div className="flex flex-col">
      <span className="text-xs font-medium uppercase tracking-wide text-zinc-400">
        {label}
      </span>
      <AnimatedNumber
        value={value}
        format={format}
        className="mt-1 text-2xl font-semibold tabular-nums text-zinc-900"
      />
    </div>
  );
}

function TotalsBand({
  overview,
  anyLive,
}: {
  overview: ArrayOwnersOverview;
  anyLive: boolean;
}) {
  const t = overview.totals;
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-6 shadow-sm sm:p-8">
      {/* Hero: lifetime value — the largest type on the screen. */}
      <div className="flex flex-col items-start">
        <span className="text-xs font-medium uppercase tracking-wide text-zinc-400">
          Lifetime value generated
        </span>
        <AnimatedNumber
          value={t.lifetime_usd}
          format={formatUsd}
          className="mt-1 text-5xl font-bold tabular-nums text-primary-700 sm:text-6xl"
        />
      </div>

      <div className="mt-6 grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
        <div className="flex flex-col">
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-400">
            Current power
          </span>
          <span className="mt-1 inline-flex items-center gap-2">
            <AnimatedNumber
              value={t.current_power_w}
              format={formatPower}
              className="text-2xl font-semibold tabular-nums text-zinc-900"
            />
            {anyLive && (
              <span className="relative flex h-2.5 w-2.5" title="Live">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary-400 opacity-75" />
                <span className="relative inline-flex h-2.5 w-2.5 rounded-full bg-primary-500" />
              </span>
            )}
          </span>
        </div>
        <TotalStat label="Today" value={t.today_kwh} format={formatKwh} />
        <TotalStat label="This month" value={t.month_kwh} format={formatKwh} />
        <TotalStat label="Lifetime" value={t.lifetime_kwh} format={formatKwh} />
      </div>
    </div>
  );
}

// ─── array card ──────────────────────────────────────────────────────────

function MetricRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="text-sm text-zinc-500">{label}</span>
      <span className="text-sm font-medium tabular-nums text-zinc-900">{value}</span>
    </div>
  );
}

function ArrayCard({
  array,
  peakWatts,
  onConnect,
}: {
  array: ArrayOwnerArray;
  peakWatts: number;
  onConnect: (a: ArrayOwnerArray) => void;
}) {
  const live = array.live;
  const isLive = live?.source === "solaredge";
  const b = array.value.breakdown;
  const noSource = array.health.status === "no_source";

  return (
    <div className="flex flex-col rounded-xl border border-zinc-200 bg-white p-6 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-zinc-900">
            {array.name}
          </h3>
          <p className="truncate text-sm text-zinc-500">{array.client_name}</p>
        </div>
        <HealthPill status={array.health.status} message={array.health.message} />
      </div>

      <div className="mt-5">
        {noSource ? (
          <div className="flex flex-col items-center gap-3 rounded-xl bg-zinc-50 py-6">
            <p className="text-sm text-zinc-500">No inverter connected</p>
            <Button
              variant="secondary"
              className="px-4 py-2"
              onClick={() => onConnect(array)}
            >
              Connect inverter
            </Button>
          </div>
        ) : (
          <PowerGauge
            watts={live?.current_power_w ?? 0}
            peakWatts={peakWatts}
            live={isLive}
          />
        )}
      </div>

      <div className="mt-5 space-y-2 border-t border-zinc-100 pt-4">
        <MetricRow
          label="Today"
          value={array.today ? formatKwh(array.today.kwh) : "—"}
        />
        <MetricRow
          label="This month"
          value={array.month ? formatKwh(array.month.kwh) : "—"}
        />
        <MetricRow
          label="Lifetime"
          value={array.lifetime ? formatKwh(array.lifetime.kwh) : "—"}
        />
      </div>

      <div className="mt-4 rounded-xl bg-primary-50/60 px-4 py-3">
        <div className="flex items-baseline justify-between">
          <span className="text-sm font-medium text-primary-800">
            Lifetime value
          </span>
          <span className="text-lg font-bold tabular-nums text-primary-700">
            {formatUsdShort(array.value.lifetime_usd)}
          </span>
        </div>
        <div className="mt-2 space-y-1 text-xs text-zinc-500">
          <div className="flex justify-between">
            <span>
              Energy{" "}
              <span className="text-zinc-400">
                @ {formatUsd(b.energy_rate_usd_per_kwh)}/kWh
              </span>
            </span>
            <span className="tabular-nums">{formatUsdShort(b.energy_usd)}</span>
          </div>
          <div className="flex justify-between">
            <span>
              RECs{" "}
              <span className="text-zinc-400">
                @ {formatUsd(b.rec_usd_per_mwh)}/MWh
              </span>
            </span>
            <span className="tabular-nums">{formatUsdShort(b.rec_usd)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── screen ──────────────────────────────────────────────────────────────

export default function ArrayOverview() {
  const toast = useToast();
  const [overview, setOverview] = useState<ArrayOwnersOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [connectTarget, setConnectTarget] = useState<ArrayOwnerArray | null>(null);

  // Per-array running peak output, so each gauge has a meaningful full-scale
  // even though the contract doesn't expose nameplate capacity. Survives polls.
  const peakRef = useRef<Map<number, number>>(new Map());

  const load = useCallback(
    async (opts: { silent?: boolean } = {}) => {
      if (!opts.silent) setLoading(true);
      try {
        const data = await arrayOwnersOverview();
        for (const a of data.arrays) {
          const w = a.live?.current_power_w ?? 0;
          const prev = peakRef.current.get(a.array_id) ?? 0;
          if (w > prev) peakRef.current.set(a.array_id, w);
        }
        setOverview(data);
        setFailed(false);
      } catch (err) {
        if (err instanceof UnauthorizedError) return; // handled globally
        // Keep stale data on screen during a transient poll failure; only show
        // the error state when we have nothing to show yet.
        if (!opts.silent) {
          setFailed(true);
          toast.error(
            err instanceof Error ? err.message : "Couldn't load your arrays",
          );
        }
      } finally {
        if (!opts.silent) setLoading(false);
      }
    },
    [toast],
  );

  // Initial load + manual retry.
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  // Poll every 60s. Silent so a blip doesn't wipe the screen.
  useEffect(() => {
    const id = window.setInterval(() => void load({ silent: true }), POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  const anyLive = useMemo(
    () =>
      overview?.arrays.some((a) => a.live?.source === "solaredge") ?? false,
    [overview],
  );

  if (loading && !overview) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center text-zinc-400">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  if (failed && !overview) {
    return (
      <div className="flex min-h-[40vh] flex-col items-center justify-center gap-4 text-center">
        <p className="text-sm text-zinc-500">Couldn&apos;t load your arrays.</p>
        <Button variant="secondary" onClick={() => setReloadKey((k) => k + 1)}>
          Try again
        </Button>
      </div>
    );
  }

  if (!overview) return null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-zinc-900">
          Array value
        </h1>
        <p className="mt-0.5 text-sm text-zinc-500">
          Live generation and dollar value across every array you own.
        </p>
      </div>

      <TotalsBand overview={overview} anyLive={anyLive} />

      {overview.arrays.length === 0 ? (
        <div className="rounded-xl border border-zinc-200 bg-white p-12 text-center shadow-sm">
          <p className="text-sm text-zinc-500">
            No arrays yet — add a client and arrays to see live value here.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {overview.arrays.map((a) => (
            <ArrayCard
              key={a.array_id}
              array={a}
              peakWatts={peakRef.current.get(a.array_id) ?? 0}
              onConnect={setConnectTarget}
            />
          ))}
        </div>
      )}

      <ConnectInverterModal
        array={connectTarget}
        onClose={() => setConnectTarget(null)}
        onConnected={() => void load({ silent: true })}
      />
    </div>
  );
}
