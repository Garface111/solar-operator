// Types for the EnergyAgent Array Owners API (v1).
//
// Mirrors docs/plans/ARRAY_OWNERS_API_CONTRACT.md exactly — the backend
// (api/array_owners.py) builds against the same document. Keep both in sync.

/** Health status of one array's data pipeline. See "Health status rules". */
export type ArrayHealthStatus = "ok" | "stale" | "offline" | "no_source";

/** Live source kind. "none" when no inverter is connected. */
export type LiveSource = "solaredge" | "none";

export interface ArrayLive {
  source: LiveSource;
  /** Instantaneous output in watts, straight from the inverter. */
  current_power_w: number;
  as_of: string; // ISO timestamp
}

export interface ArrayKwh {
  kwh: number;
}

export interface ArrayValueBreakdown {
  /** Retail offset rate used for the energy $ component. */
  energy_rate_usd_per_kwh: number;
  /** REC market price used for the REC $ component. */
  rec_usd_per_mwh: number;
  /** generation × rate */
  energy_usd: number;
  /** floor(MWh) × rec price (lifetime); pro-rated for today/month. */
  rec_usd: number;
}

export interface ArrayValue {
  today_usd: number;
  month_usd: number;
  lifetime_usd: number;
  breakdown: ArrayValueBreakdown;
}

export interface ArrayHealth {
  status: ArrayHealthStatus;
  last_data_day: string | null; // YYYY-MM-DD
  days_since_data: number | null;
  message: string;
}

export interface ArrayOwnerArray {
  array_id: number;
  name: string;
  client_name: string;
  fuel_type: string; // "solar" | "wind" | ...
  /** null when no live source is connected. */
  live: ArrayLive | null;
  /** null when no daily data exists for the period. */
  today: ArrayKwh | null;
  month: ArrayKwh | null;
  lifetime: ArrayKwh | null;
  value: ArrayValue;
  health: ArrayHealth;
}

export interface ArrayOwnerTotals {
  current_power_w: number;
  today_kwh: number;
  month_kwh: number;
  lifetime_kwh: number;
  today_usd: number;
  month_usd: number;
  lifetime_usd: number;
}

/** GET /v1/array-owners/overview response. */
export interface ArrayOwnersOverview {
  generated_at: string; // ISO timestamp
  arrays: ArrayOwnerArray[];
  totals: ArrayOwnerTotals;
}

/** POST /v1/array-owners/arrays/{id}/solaredge success body. */
export interface ConnectSolarEdgeResult {
  ok: boolean;
  site_name: string;
  peak_power_kw: number;
}

// ── account-level SolarEdge discovery ("paste one credential, attach all") ──

/** One site returned by POST /v1/array-owners/solaredge/discover. */
export interface SolarEdgeDiscoveredSite {
  site_id: number;
  name: string;
  peak_power_kw: number | null;
  status: string;
}

/** POST /v1/array-owners/solaredge/discover response (preview — saves nothing). */
export interface SolarEdgeDiscoverResult {
  ok: boolean;
  sites: SolarEdgeDiscoveredSite[];
  /** Friendly note for the empty-account case; null when sites were found. */
  message: string | null;
}

/** One array attached by connect-account (created or matched). */
export interface ConnectAccountEntry {
  array_id: number | null;
  name: string;
  site_id: number;
  peak_power_kw: number | null;
}

/** POST /v1/array-owners/solaredge/connect-account response. */
export interface ConnectAccountResult {
  ok: boolean;
  /** Every site attached this run (created ∪ matched). */
  connected: ConnectAccountEntry[];
  /** Arrays freshly created from a SolarEdge site. */
  created: ConnectAccountEntry[];
  /** Existing arrays matched to a site (by site_id or exact name). */
  matched: ConnectAccountEntry[];
  message: string;
}
