import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Checkbox } from "../ui/Checkbox";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  arrayOwnersOverview,
  connectSolarEdge,
  connectSolarEdgeAccount,
  discoverSolarEdge,
  UnauthorizedError,
} from "../lib/api";
import type {
  ArrayHealthStatus,
  ArrayLive,
  ArrayOwnerArray,
  ArrayOwnersOverview,
  ConnectAccountResult,
  SolarEdgeDiscoveredSite,
} from "../lib/arrayOwners";
import { timeAgo } from "../components/settings/utils";

const POLL_MS = 60_000;

/** A reading older than this is stale — never rendered as "live". */
const LIVE_FRESH_MS = 15 * 60_000;

/**
 * Freshness gate for the "live" visual state: source must be solaredge AND the
 * reading's as_of must be within the last 15 minutes. Payloads without as_of
 * fall back to trusting the source flag (backward compat).
 */
function liveFreshness(live: ArrayLive | null): {
  isLive: boolean;
  asOf: Date | null;
} {
  if (live?.source !== "solaredge") return { isLive: false, asOf: null };
  if (!live.as_of) return { isLive: true, asOf: null };
  const asOf = new Date(live.as_of);
  if (Number.isNaN(asOf.getTime())) return { isLive: true, asOf: null };
  return { isLive: Date.now() - asOf.getTime() <= LIVE_FRESH_MS, asOf };
}

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
  /** True only when the reading is fresh (see liveFreshness). */
  live: boolean;
  /** When the reading was taken; shown so stale data is never mistaken for live. */
  asOf: Date | null;
}

/**
 * Semicircular SVG arc gauge — no chart libraries. The arc fills proportional
 * to current output against `peakWatts` (the largest output we've seen for this
 * array this session, since the contract doesn't expose nameplate capacity).
 */
function PowerGauge({ watts, peakWatts, live, asOf }: PowerGaugeProps) {
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
          className={[
            "text-xl font-semibold tabular-nums",
            live ? "text-zinc-900" : "text-zinc-400",
          ].join(" ")}
        />
        <span className="text-[10px] uppercase tracking-wide text-zinc-400">
          {live ? "live output" : asOf ? `as of ${timeAgo(asOf)}` : "no live data"}
        </span>
        {live && asOf && (
          <span className="text-[10px] text-zinc-400">as of {timeAgo(asOf)}</span>
        )}
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

/** kW formatter for the site picker rows. */
function formatKw(kw: number | null): string {
  if (kw === null || kw === undefined) return "—";
  return `${kw.toLocaleString("en-US", { maximumFractionDigits: 2 })} kW`;
}

type ConnectStep = "key" | "sites" | "done";

/**
 * Account-first SolarEdge connect.
 *
 * Step 1: paste ONE account-level API key.
 * Step 2: we discover every site on the account and show them as checkboxes.
 * Step 3: connect-account attaches them all at once, then a celebratory summary.
 *
 * The old per-array "single site" flow (api key + site id) is kept under a
 * small link — this is the fallback the real pilot needed when a site-level
 * key can't enumerate.
 */
function ConnectInverterModal({ array, onClose, onConnected }: ConnectModalProps) {
  const toast = useToast();
  const [apiKey, setApiKey] = useState("");
  const [step, setStep] = useState<ConnectStep>("key");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Discovery (step 2).
  const [sites, setSites] = useState<SolarEdgeDiscoveredSite[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  // Celebration (step 3).
  const [result, setResult] = useState<ConnectAccountResult | null>(null);

  // Manual single-site fallback path.
  const [manual, setManual] = useState(false);
  const [siteId, setSiteId] = useState("");

  // Reset everything whenever a different array opens the modal.
  useEffect(() => {
    setApiKey("");
    setStep("key");
    setBusy(false);
    setError(null);
    setSites([]);
    setSelected(new Set());
    setResult(null);
    setManual(false);
    setSiteId("");
  }, [array?.array_id]);

  const open = array !== null;
  const siteIdNum = Number(siteId.trim());
  const manualValid =
    apiKey.trim().length > 0 &&
    siteId.trim().length > 0 &&
    Number.isInteger(siteIdNum) &&
    siteIdNum > 0;

  async function handleDiscover() {
    if (!apiKey.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await discoverSolarEdge(apiKey.trim());
      if (res.sites.length === 0) {
        setError(res.message || "No sites found on this SolarEdge account.");
        return;
      }
      setSites(res.sites);
      setSelected(new Set(res.sites.map((s) => s.site_id))); // all checked
      setStep("sites");
    } catch (err) {
      if (err instanceof UnauthorizedError) return; // handled globally
      // 400s carry the server's actionable guidance (e.g. "use an account-level
      // key") — surface inline so the operator can fix it, not a fleeting toast.
      setError(err instanceof Error ? err.message : "Couldn't reach SolarEdge");
    } finally {
      setBusy(false);
    }
  }

  async function handleConnectAccount() {
    if (busy || selected.size === 0) return;
    setBusy(true);
    setError(null);
    try {
      const res = await connectSolarEdgeAccount(apiKey.trim(), [...selected]);
      setResult(res);
      setStep("done");
      onConnected(); // refresh the overview underneath
    } catch (err) {
      if (err instanceof UnauthorizedError) return;
      setError(err instanceof Error ? err.message : "Couldn't connect your sites");
    } finally {
      setBusy(false);
    }
  }

  async function handleManualConnect() {
    if (!array || !manualValid || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await connectSolarEdge(array.array_id, apiKey.trim(), siteIdNum);
      toast.success(`Connected ${res.site_name}`);
      onConnected();
      onClose();
    } catch (err) {
      if (err instanceof UnauthorizedError) return;
      setError(err instanceof Error ? err.message : "Couldn't connect the inverter");
    } finally {
      setBusy(false);
    }
  }

  function toggleSite(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const title = manual
    ? array
      ? `Connect a single site — ${array.name}`
      : "Connect a single site"
    : step === "done"
      ? "SolarEdge connected"
      : "Connect SolarEdge";

  // ── footer (varies by step / mode) ──
  let footer: ReactNode;
  if (manual) {
    footer = (
      <>
        <Button variant="ghost" onClick={() => setManual(false)} disabled={busy}>
          Back
        </Button>
        <Button onClick={handleManualConnect} disabled={!manualValid || busy}>
          {busy ? (
            <>
              <Spinner />
              Connecting…
            </>
          ) : (
            "Connect"
          )}
        </Button>
      </>
    );
  } else if (step === "key") {
    footer = (
      <>
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={handleDiscover} disabled={!apiKey.trim() || busy}>
          {busy ? (
            <>
              <Spinner />
              Finding sites…
            </>
          ) : (
            "Discover my sites"
          )}
        </Button>
      </>
    );
  } else if (step === "sites") {
    footer = (
      <>
        <Button
          variant="ghost"
          onClick={() => {
            setStep("key");
            setError(null);
          }}
          disabled={busy}
        >
          Back
        </Button>
        <Button onClick={handleConnectAccount} disabled={selected.size === 0 || busy}>
          {busy ? (
            <>
              <Spinner />
              Connecting…
            </>
          ) : (
            `Connect ${selected.size} ${selected.size === 1 ? "array" : "arrays"}`
          )}
        </Button>
      </>
    );
  } else {
    footer = (
      <Button onClick={onClose}>Done</Button>
    );
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      title={title}
      footer={footer}
    >
      {/* ── manual single-site fallback ── */}
      {manual ? (
        <div className="space-y-4">
          <p className="text-sm text-zinc-500">
            Paste your SolarEdge API key and the site ID for this array. We
            validate the key with SolarEdge before saving.
          </p>
          <Input
            id="se-api-key-manual"
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
      ) : step === "key" ? (
        /* ── step 1: one field ── */
        <div className="space-y-4">
          <p className="text-sm text-zinc-500">
            Paste your <span className="font-medium text-zinc-700">account-level</span>{" "}
            SolarEdge API key. We&apos;ll find every site on your account and
            connect them all at once — no need to do one array at a time.
          </p>
          <Input
            id="se-api-key"
            label="SolarEdge API key"
            autoFocus
            placeholder="e.g. ABCD1234EFGH5678"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            error={error ?? undefined}
          />
          <p className="text-xs text-zinc-400">
            Find it in the SolarEdge monitoring portal under Admin → Site
            Access → API Access. An account key lists every site; a site key
            only covers one.
          </p>
          <button
            type="button"
            className="text-xs font-medium text-primary-600 hover:text-primary-700"
            onClick={() => {
              setManual(true);
              setError(null);
            }}
          >
            Connect a single site manually
          </button>
        </div>
      ) : step === "sites" ? (
        /* ── step 2: pick sites ── */
        <div className="space-y-4">
          <p className="text-sm text-zinc-500">
            We found{" "}
            <span className="font-medium text-zinc-700">
              {sites.length} {sites.length === 1 ? "site" : "sites"}
            </span>{" "}
            on your account. Choose which to connect.
          </p>
          <div className="max-h-72 space-y-1 overflow-y-auto rounded-xl border border-zinc-200 p-1">
            {sites.map((s) => (
              <label
                key={s.site_id}
                className="flex cursor-pointer items-center justify-between gap-3 rounded-lg px-3 py-2.5 hover:bg-zinc-50"
              >
                <span className="flex min-w-0 items-center gap-2.5">
                  <Checkbox
                    id={`se-site-${s.site_id}`}
                    checked={selected.has(s.site_id)}
                    onChange={() => toggleSite(s.site_id)}
                  />
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-medium text-zinc-900">
                      {s.name || `Site ${s.site_id}`}
                    </span>
                    <span className="block text-xs text-zinc-400">
                      Site {s.site_id}
                    </span>
                  </span>
                </span>
                <span className="shrink-0 text-sm tabular-nums text-zinc-500">
                  {formatKw(s.peak_power_kw)}
                </span>
              </label>
            ))}
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <button
            type="button"
            className="text-xs font-medium text-primary-600 hover:text-primary-700"
            onClick={() =>
              setSelected((prev) =>
                prev.size === sites.length
                  ? new Set()
                  : new Set(sites.map((s) => s.site_id)),
              )
            }
          >
            {selected.size === sites.length ? "Clear all" : "Select all"}
          </button>
        </div>
      ) : (
        /* ── step 3: celebration ── */
        <div className="flex flex-col items-center gap-3 py-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary-50">
            <svg
              viewBox="0 0 24 24"
              className="h-6 w-6 text-primary-600"
              fill="none"
              stroke="currentColor"
              strokeWidth={2.5}
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden
            >
              <path d="M20 6 9 17l-5-5" />
            </svg>
          </div>
          <p className="text-lg font-semibold text-zinc-900">
            {result?.connected.length ?? 0}{" "}
            {(result?.connected.length ?? 0) === 1 ? "array" : "arrays"} connected
          </p>
          <p className="text-sm text-zinc-500">
            {result?.created.length ?? 0} new · {result?.matched.length ?? 0}{" "}
            matched to existing arrays
          </p>
        </div>
      )}
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
  // "Updated X ago" — the payload's own generation time, so the user can tell
  // a fresh screen from one served out of a stalled backend.
  const generatedDate = new Date(overview.generated_at);
  const generatedAt = Number.isNaN(generatedDate.getTime()) ? null : generatedDate;
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

      {generatedAt && (
        <p className="mt-4 text-xs text-zinc-400">
          Updated {timeAgo(generatedAt)}
        </p>
      )}
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
  const { isLive, asOf } = liveFreshness(live);
  const b = array.value.breakdown;
  const noSource = array.health.status === "no_source";
  const staleDays = array.health.days_since_data;

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
            asOf={asOf}
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
        {staleDays != null && staleDays >= 2 && array.health.last_data_day && (
          <p className="text-xs text-zinc-400">
            data through {array.health.last_data_day}
          </p>
        )}
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
    () => overview?.arrays.some((a) => liveFreshness(a.live).isLive) ?? false,
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
