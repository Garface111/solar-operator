// Dashboard API client + session handling.
//
// The SPA is served same-origin (nepooloperator.com/accounts, Netlify-proxied to
// the FastAPI /app/ mount; /v1/* is proxied too), so every call is a
// relative-path fetch. The session token lives in localStorage under
// 'so_session' and is sent as `Authorization: Bearer <token>` on /v1/account/*.
//
// On a 401 we clear the session and broadcast a window event so the app shell
// can bounce to the login screen from anywhere.

import type {
  ArrayOwnersOverview,
  ConnectAccountResult,
  ConnectSolarEdgeResult,
  SolarEdgeDiscoverResult,
} from "./arrayOwners";

const SESSION_KEY = "so_session";
export const UNAUTHORIZED_EVENT = "so-unauthorized";

export function getSession(): string | null {
  return localStorage.getItem(SESSION_KEY);
}

export function setSession(token: string): void {
  localStorage.setItem(SESSION_KEY, token);
  // A fresh session re-arms the one-shot 401 notifier so the NEXT genuine
  // expiry surfaces its bounce-to-login again.
  unauthorizedNotified = false;
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}

// One-shot guard: a single expired session typically fans out into several
// concurrent authed requests (account + clients + reports), each returning 401.
// Without this, every one of them dispatches UNAUTHORIZED_EVENT and the user
// gets a STACK of identical "session expired" toasts. Notify once per session;
// setSession() re-arms it on the next sign-in.
let unauthorizedNotified = false;

/** Broadcast a single session-expiry bounce. Idempotent until the next login. */
function notifyUnauthorizedOnce(): void {
  clearSession();
  if (unauthorizedNotified) return;
  unauthorizedNotified = true;
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
}

/** Raised on a 401 so callers can distinguish auth failures from other errors. */
export class UnauthorizedError extends Error {
  constructor() {
    super("Session expired — sign in again");
    this.name = "UnauthorizedError";
  }
}

/** Raised on a structured 409 conflict so callers can react (e.g. offer
 *  "Open existing client" instead of a generic error toast). */
export class ConflictError extends Error {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  detail: any;
  constructor(message: string, detail: unknown) {
    super(message);
    this.name = "ConflictError";
    this.detail = detail;
  }
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail))
      return body.detail.map((d: any) => d.msg).join("; ");
    if (body?.detail && typeof body.detail === "object") {
      // Structured 409 — surface the human-readable .message but stash the
      // whole detail on a ConflictError below so callers can read it.
      return body.detail.message || JSON.stringify(body.detail);
    }
  } catch {
    /* fall through */
  }
  return `Request failed (${res.status})`;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Skip the Authorization header (used by the public auth endpoints). */
  noAuth?: boolean;
}

/** Default per-request timeout. A stalled connection should surface an error,
 *  not a forever-spinner or a permanently-disabled button. */
const DEFAULT_TIMEOUT_MS = 30_000;
const TIMEOUT_MESSAGE =
  "Request timed out — check your connection and try again.";

/** fetch() with an AbortController timeout. Translates the abort into a clear,
 *  user-facing error instead of a bare DOMException. */
export async function fetchWithTimeout(
  input: RequestInfo,
  init: RequestInit = {},
  ms: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(TIMEOUT_MESSAGE);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (!opts.noAuth) {
    const token = getSession();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetchWithTimeout(path, {
    method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  if (res.status === 401) {
    // A 401 on an AUTHED request means the session token is dead → bounce to
    // login. But a 401 on a noAuth request (the public auth endpoints:
    // password-login, auth/verify, auth/request) means BAD CREDENTIALS, not an
    // expired session — there is no session yet. Surface the server's real
    // message ("Invalid email or password") inline instead of firing the
    // session-expiry machinery, which would mislabel a wrong password as
    // "Session expired — sign in again" and bounce the login screen.
    if (!opts.noAuth) {
      notifyUnauthorizedOnce();
      throw new UnauthorizedError();
    }
    throw new Error(await parseError(res));
  }
  if (!res.ok) {
    // Structured 409 (e.g. login-already-claimed) — surface the detail
    // object so the UI can offer "Open existing client" instead of a
    // dead-end error toast.
    if (res.status === 409) {
      let body: any = null;
      try { body = await res.clone().json(); } catch { /* ignore */ }
      if (body?.detail && typeof body.detail === "object") {
        throw new ConflictError(
          body.detail.message || "Conflict",
          body.detail,
        );
      }
    }
    throw new Error(await parseError(res));
  }
  const text = await res.text();
  return (text ? JSON.parse(text) : {}) as T;
}

// ─── auth ──────────────────────────────────────────────────────────────────

// This bundle is the NEPOOL Operator dashboard (served at nepooloperator.com
// /accounts). Every auth call declares its product so an email that owns BOTH a
// NEPOOL and an Array Operator account is routed to its NEPOOL tenant — never
// cross-leaked into Array Operator (which has its own login passing
// product:"array_operator"). See api/account.issue_magic_link strict scoping.
const PRODUCT = "nepool";

export async function requestLoginLink(email: string, persist = true): Promise<void> {
  await request("/v1/auth/request", {
    body: { email, persist, product: PRODUCT },
    noAuth: true,
  });
}

/** Sign in with email + password. Returns session token on success. */
export async function passwordLogin(
  email: string,
  password: string,
): Promise<string> {
  const res = await request<{ session_token: string }>(
    "/v1/auth/password-login",
    { body: { email, password, product: PRODUCT }, noAuth: true },
  );
  return res.session_token;
}

/** Set or change the signed-in operator's password.
 *  currentPassword is required when changing an existing password. */
export async function setPassword(
  password: string,
  currentPassword?: string,
): Promise<void> {
  await request<{ ok: boolean }>("/v1/auth/set-password", {
    body: { password, ...(currentPassword ? { current_password: currentPassword } : {}) },
  });
}

export async function verifyLoginToken(token: string): Promise<string> {
  const res = await request<{ session_token: string }>("/v1/auth/verify", {
    body: { token },
    noAuth: true,
  });
  return res.session_token;
}

// ─── types ───────────────────────────────────────────────────────────────

export interface UtilitySessionStatus {
  captured_at: string | null;
  expires_at: string | null;
  last_refresh_at: string | null;
  refresh_failures: number;
}

export interface Account {
  tenant_id: string;
  tenant_key: string | null;
  name: string | null;
  operator_name: string | null;
  company_name: string | null;
  email: string | null;
  /** Which product this tenant belongs to: "nepool" (NEPOOL Operator) or
   *  "array_operator" (Array Operator). Drives shell branding + tab labels so
   *  an Array Operator owner never sees NEPOOL chrome. */
  product: string | null;
  plan: string | null;
  active: boolean;
  /** Shared read-only demo tenant. When true the SPA shows the demo banner and
   *  hides the Mind button; mutating API calls return a 403 demo-read-only. */
  is_demo: boolean;
  subscription_status: string | null;
  report_frequency: string | null;
  cc_on_reports: boolean;
  has_password: boolean;
  // V2 email customization. null template fields mean "use the built-in default".
  send_from_email: string | null;
  send_from_name: string | null;
  email_subject_template: string | null;
  email_body_template: string | null;
  send_mode: string; // "to_client" | "to_me" | "to_both"
  default_email_subject: string;
  default_email_body: string;
  merge_tags: string[];
  last_pull_at: string | null;
  last_delivery_at: string | null;
  extension_heartbeat_at: string | null;
  /** Server-side Auto-refresh preference: "cloud" | "device" | null (legacy). */
  capture_mode?: string | null;
  created_at: string | null;
  trial_ends_at: string | null;
  /** No-upfront-payment: true once the operator has added a card. A live
   *  trialing tenant can have this false. Drives the trial-banner CTA and the
   *  read-only pause gating. */
  has_payment_method: boolean;
  accounts_count: number;
  /** Distinct provider codes this tenant has utility accounts for — the true
   *  "connected portals" set. Drives the Live portals list so it shows only
   *  the operator's connected utilities, not the whole national catalog. */
  connected_providers?: string[];
  bills_count: number;
  clients_count: number;
  onboarding_array_estimate: number | null;
  all_set: boolean;
  session: UtilitySessionStatus | null;
}

export interface EmailSettingsInput {
  send_from_email?: string | null;
  send_from_name?: string | null;
  email_subject_template?: string | null;
  email_body_template?: string | null;
  send_mode?: string | null;
}

export interface EmailSettings {
  send_from_email: string | null;
  send_from_name: string | null;
  email_subject_template: string | null;
  email_body_template: string | null;
  send_mode: string;
}

export interface EmailPreview {
  subject: string;
  html: string;
  text: string;
  from: string;
  to: string;
  send_mode: string;
}

export interface UtilityAccount {
  id: number;
  provider: string;
  provider_label: string;
  account_number: string;
  customer_number?: string | null;
  nickname: string | null;
  /** ISO timestamp of the last capture/sync that touched this account
   *  (server-side `UtilityAccount.last_seen`). Null if never synced.
   *  Powers the Capture Freshness Heatmap. */
  last_synced_at?: string | null;
}

export interface ArrayRow {
  id: number;
  name: string;
  nepool_gis_id: string | null;
  region: string | null;
  bill_offset_months: number | null;
  /** V2: generation source — solar|wind|hydro|digester|storage. Backend
   *  defaults to 'solar'; treat a missing value as solar. */
  fuel_type?: string | null;
  notes: string | null;
  excluded: boolean;
  accounts: UtilityAccount[];
  solaredge_connected: boolean;
  solaredge_site_id: number | null;
  /** ISO timestamp set when the array is soft-deleted. Null for active arrays. */
  deleted_at?: string | null;
}

export interface ClientRow {
  id: number;
  name: string;
  contact_email: string | null;
  cc_emails: string | null;
  report_frequency: string | null;
  active: boolean;
  array_count: number;
  last_delivery_at: string | null;
  notes: string | null;
  gmp_email: string | null;
  gmp_username: string | null;
  gmp_autopopulate: boolean;
  gmp_last_sync_at: string | null;
  vec_email: string | null;
  vec_username: string | null;
  vec_autopopulate: boolean;
  vec_last_sync_at: string | null;
  last_delivered_at: string | null;
  last_bounced_at: string | null;
  last_bounce_reason: string | null;
  /** True when the onboarding flow seeded this client as a "Your first
   *  client" placeholder. Cleared the moment the operator renames it
   *  OR arrays land from a portal capture. */
  is_placeholder?: boolean;
  /** True when this client was eagerly created from a stored Cloud Capture
   *  login and is still awaiting its first harvested bill. Drives the
   *  "Pulling bills…" state; cleared on the first capture. */
  capture_pending?: boolean;
  /** Most recent time we SUCCESSFULLY signed in and checked this client's
   *  utility (a cloud-capture harvest that authenticated OK) — even if no new
   *  bill landed. Drives the "Last checked" column so a monthly-billing meter
   *  doesn't look stale between bills. */
  last_checked_at?: string | null;
  /** THE FOLD: when TRUE this client's generation report auto-sends each period
   *  — and is the operator's opt-in to the $15/client/quarter charge (the meter
   *  fires on the first real output). Default false; the operator flips it per
   *  client in the roster. Manual send/download still work regardless. */
  auto_send?: boolean;
}

// ─── account ───────────────────────────────────────────────────────────────

/** THE FOLD: SSE endpoint resolver. On arrayoperator.com the Netlify /v1
 *  proxy BUFFERS event-streams (~21s to first byte, measured 2026-07-16), so
 *  the embed entry points SSE — and only SSE — straight at the Railway
 *  origin (window.__soEventsBase; CORS + AO's CSP connect-src both already
 *  allow it). The standalone SPA leaves the global unset → same-origin. */
export function eventsUrl(): string {
  const base = (window as unknown as { __soEventsBase?: string }).__soEventsBase;
  return (typeof base === "string" && base ? base.replace(/\/+$/, "") : "") + "/v1/events";
}

export function getAccount(): Promise<Account> {
  return request<Account>("/v1/account");
}

export async function updateAccountEmail(email: string): Promise<string> {
  const res = await request<{ email: string }>("/v1/account/email", {
    body: { email },
  });
  return res.email;
}

export async function updateAccountName(name: string): Promise<string> {
  const res = await request<{ name: string }>("/v1/account/name", {
    body: { name },
  });
  return res.name;
}

export async function updateAccountCompanyName(name: string): Promise<string> {
  const res = await request<{ name: string }>("/v1/account/company-name", {
    body: { name },
  });
  return res.name;
}

export async function updateAccountSendFromName(
  sendFromName: string | null,
): Promise<string | null> {
  const res = await request<{ send_from_name: string | null }>(
    "/v1/account/send-from-name",
    { body: { send_from_name: sendFromName } },
  );
  return res.send_from_name;
}

export interface UtilityRequestInput {
  utility_name: string;
  portal_url?: string | null;
  region?: string | null;
  notes?: string | null;
}

/** Submit a "don't see your utility?" request. Emails the SO team and, when a
 *  Hermes agent webhook is configured server-side, kicks off an autonomous
 *  agent run that adds the utility to the repo and opens a PR. */
export async function requestUtilityAddition(
  input: UtilityRequestInput,
): Promise<{ ok: boolean; agent_dispatched: boolean }> {
  return request<{ ok: boolean; agent_dispatched: boolean }>(
    "/v1/account/request-utility",
    { body: input },
  );
}

export async function updateAccountFrequency(
  frequency: string,
): Promise<string> {
  const res = await request<{ frequency: string }>("/v1/account/frequency", {
    body: { frequency },
  });
  return res.frequency;
}

// ─── provider catalog ────────────────────────────────────────────────────

export interface ProviderEntry {
  code: string;
  label: string;
  state: string;
  scrape_status: "live" | "in-progress" | "manual";
  smarthub_host: string;
  portal_url: string;
  notes: string;
}

/** The full supported-utility catalog (public; drives the Add-a-client portal
 *  list). Sourced from api/data/providers/*.csv via GET /v1/providers, so new
 *  utilities appear here the moment the backend deploys — no frontend rebuild. */
export async function getProviders(): Promise<ProviderEntry[]> {
  const res = await request<{ ok: boolean; providers: ProviderEntry[] }>(
    "/v1/providers",
    { noAuth: true },
  );
  return res.providers;
}

export async function getBillingPortalUrl(): Promise<string> {
  const res = await request<{ url: string }>("/v1/account/billing-portal");
  return res.url;
}

// ── Cloud Capture (server-side portal harvesting) ───────────────────────────
// Product-agnostic backend; NEPOOL uses this for utility-bill vault + harvest.
// Passwords are write-only — no endpoint ever returns them.
export interface CloudCredential {
  provider: string;
  username: string;
  enabled: boolean;
  login_host: string | null;
  last_harvest_at: string | null;
  last_harvest_ok: boolean | null;
  /** Newest HarvestRun status for this login (ok | login_failed | scrape_failed | …). */
  last_harvest_status?: string | null;
  harvest_fails: number;
  has_session?: boolean;
}
export interface CloudCaptureStatus {
  encryption_ready?: boolean;
  collection_enabled?: boolean;
  harvesting_enabled?: boolean;
  credentials: CloudCredential[];
}
export async function getCloudCaptureStatus(): Promise<CloudCaptureStatus> {
  return request<CloudCaptureStatus>("/v1/cloud-capture/status");
}
export interface CloudCredentialInput {
  provider: string;
  username: string;
  password?: string;
  login_host?: string | null;
  enable?: boolean;
  consent?: boolean;
}
export async function setCloudCredential(
  input: CloudCredentialInput,
): Promise<{ ok: boolean; provider?: string; username?: string }> {
  return request<{ ok: boolean; provider?: string; username?: string }>(
    "/v1/cloud-capture/credentials",
    { method: "POST", body: input },
  );
}
export async function deleteCloudCredential(
  provider: string,
  username: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/v1/cloud-capture/credentials", {
    method: "DELETE",
    body: { provider, username },
  });
}
export async function toggleCloudCredential(
  provider: string,
  username: string,
  enable: boolean,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/v1/cloud-capture/toggle", {
    method: "POST",
    body: { provider, username, enable },
  });
}
export async function refreshCloudCapture(): Promise<{ ok: boolean; queued: number }> {
  return request<{ ok: boolean; queued: number }>("/v1/cloud-capture/refresh", {
    method: "POST",
    body: {},
  });
}
export async function setCaptureMode(
  mode: "cloud" | "device",
): Promise<{ ok: boolean; capture_mode: string }> {
  return request<{ ok: boolean; capture_mode: string }>("/v1/account/capture-mode", {
    method: "POST",
    body: { mode },
  });
}

export interface BillingSummary {
  billable_arrays: number;
  price_cents: number;
  /** Undiscounted per-array price ($15). Present when volume tiers are live. */
  full_unit_cents?: number;
  total_cents: number;
  currency: string;
  /** No-upfront-payment: a live (trialing) tenant can have no card on file.
   *  Drives the "Add payment method" CTA and the paused-no-card banner. */
  has_payment_method: boolean;
  /** Which plan shape to render: "array" (NEPOOL per-array), "kwh" (Array Operator
   *  monitoring), or "invoicing" (Array Operator per-offtaker). Absent on legacy
   *  responses → render as "array". The two AO bases carry the dual-model fields. */
  billing_basis?: "array" | "kwh" | "invoicing";
  // ── Array Operator dual model (present when billing_basis is "kwh" / "invoicing") ──
  /** Monitoring (per-kWh) plan. */
  mtd_kwh?: number;
  rate_cents_per_kwh?: number; // decimal cents per kWh (e.g. 0.5)
  blended_cents_per_kwh?: number;
  monitoring_total_cents?: number; // month-to-date, decimal cents
  /** Invoicing (per-offtaker) plan. */
  offtaker_count?: number;
  invoicing_base_cents?: number; // $100 base
  invoicing_base_includes?: number; // offtakers the base covers (4)
  invoicing_per_offtaker_cents?: number; // $25 beyond the base
  invoicing_setup_cents?: number; // $250 one-time
  invoicing_total_cents?: number;
}

/** What the tenant is billed for: array count (the Stripe quantity) × per-array
 *  price. Lets the Account tab show the real monthly figure. */
export async function getBillingSummary(): Promise<BillingSummary> {
  return request<BillingSummary>("/v1/account/billing-summary");
}

/** Start the add-card flow: returns a Stripe Checkout setup-mode URL and
 *  redirects the browser to it. The setup_intent.succeeded webhook attributes
 *  the saved card back to the tenant (and auto-resumes a paused account). */
export async function addPaymentMethod(): Promise<void> {
  const res = await request<{ checkout_url: string }>(
    "/v1/account/add-payment-method",
    { method: "POST" },
  );
  window.location.href = res.checkout_url;
}

/** Confirm a completed Checkout setup session the moment the operator lands
 *  back (?card_added=1&session_id=…). Attribution-only on the backend: stores
 *  the card on the tenant synchronously so the UI can confirm "card saved"
 *  without racing the setup_intent.succeeded webhook. */
export async function confirmCardSetup(sessionId: string): Promise<{
  ok: boolean;
  card_saved: boolean;
  card_brand: string | null;
  card_last4: string | null;
  trial_ends_at: string | null;
}> {
  return request("/v1/account/confirm-setup", {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

/** Restart a CANCELLED account: returns a Stripe Checkout setup-mode URL and
 *  redirects to it. The setup_intent.succeeded webhook recaptures the card and
 *  starts a fresh PAID subscription with NO trial (create_subscription_for_tenant),
 *  flipping the tenant back to active. Gated server-side to cancelled tenants. */
export async function reactivateAccount(): Promise<void> {
  const res = await request<{ checkout_url: string }>(
    "/v1/account/reactivate",
    { method: "POST" },
  );
  window.location.href = res.checkout_url;
}
/** Manual fallback to resume a 'paused_no_card' tenant once a card is on file.
 *  Normally the webhook resumes automatically right after the card is added. */
export async function resumeFromPause(): Promise<{
  ok: boolean;
  subscription_status: string;
  active: boolean;
}> {
  return request("/v1/account/resume-from-pause", { method: "POST" });
}

export interface CaptureEntry {
  pulled_at: string | null;
  client_name: string;
  array_name: string;
  period_start: string | null;
  period_end: string | null;
}

/** Last N bill captures for this tenant, annotated with client + array names. */
export async function getRecentCaptures(limit = 5): Promise<CaptureEntry[]> {
  const res = await request<{ captures: CaptureEntry[] }>(
    `/v1/account/recent-captures?limit=${limit}`,
  );
  return res.captures;
}

export interface NextInvoice {
  amount_cents: number | null;
  currency: string | null;
  period_end: string | null;
}

/** Next Stripe invoice amount + due date for the billing strip. */
export async function getNextInvoice(): Promise<NextInvoice> {
  return request<NextInvoice>("/v1/account/next-invoice");
}

export interface FromDomainStatus {
  domain: string | null;
  status: "verified" | "pending" | "unverified" | "unknown" | "none";
}

/** Check Resend verification status for the tenant's custom send_from_email domain. */
export async function getFromDomainStatus(): Promise<FromDomainStatus> {
  return request<FromDomainStatus>("/v1/account/from-domain-status");
}

/** Cancel a trial in progress — detaches payment method and tombstones the account. */
export async function cancelTrial(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/v1/onboarding/cancel-trial", {
    method: "POST",
  });
}

/** Toggle 'send me a copy of every report'. Returns the updated value. */
export async function updateCcOnReports(
  ccOnReports: boolean,
): Promise<boolean> {
  const res = await request<{ cc_on_reports: boolean }>(
    "/v1/account/cc-on-reports",
    { body: { cc_on_reports: ccOnReports } },
  );
  return res.cc_on_reports;
}

/** One client's outcome from a send-report run. */
export interface SendResult {
  client_id: number;
  client_name: string | null;
  ok: boolean;
  recipient: string;
  reason?: string;
}

export interface SendReportResult {
  ok: boolean;
  client_count: number;
  delivered: number;
  results: SendResult[];
}

/** Trigger an immediate report send. With no clientIds, fans out to every
 *  active client (legacy). With clientIds, sends only to the chosen subset.
 *  sendMode, when provided, is saved as the tenant default before delivery.
 *  Returns the per-client outcome so the UI can tell the truth about partial
 *  or total failures instead of a blanket success toast. */
export async function sendReportNow(
  clientIds?: number[],
  sendMode?: string,
): Promise<SendReportResult & { directory?: { ok?: boolean; sheet_count?: number; recipient?: string; reason?: string } }> {
  const payload: Record<string, unknown> = {};
  if (clientIds && clientIds.length > 0) payload.client_ids = clientIds;
  if (sendMode) payload.send_mode = sendMode;
  const hasPayload = Object.keys(payload).length > 0;
  return request("/v1/account/send-report", {
    method: "POST",
    ...(hasPayload ? { body: payload } : {}),
  });
}

/** Download the operator NEPOOL-GIS directory (all clients × arrays). */
export async function downloadDirectoryReport(quarter?: string): Promise<void> {
  const token = getSession();
  const qs = quarter ? `?quarter=${encodeURIComponent(quarter)}` : "";
  const res = await fetchWithTimeout(
    `/v1/account/directory-report.xlsx${qs}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `Couldn't build the directory (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(typeof msg === "string" ? msg : "Couldn't build the directory");
  }
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  const label = quarter ? quarter.replace(/[^A-Za-z0-9]+/g, "-") : "latest";
  link.download = `NEPOOL-directory-${label}.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/** Download ALL clients' utility generation in one workbook (a Generation
 *  Summary across GMP + co-ops, plus per-project daily meter detail). The
 *  all-clients counterpart of downloadGeneration(). */
export async function downloadGenerationDirectory(quarter?: string): Promise<void> {
  const token = getSession();
  const qs = quarter ? `?quarter=${encodeURIComponent(quarter)}` : "";
  const res = await fetchWithTimeout(
    `/v1/account/generation-directory.xlsx${qs}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `Couldn't build the generation directory (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(typeof msg === "string" ? msg : "Couldn't build the generation directory");
  }
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  const label = quarter ? quarter.replace(/[^A-Za-z0-9]+/g, "-") : "latest";
  link.download = `generation-directory-${label}.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/** Email the operator their NEPOOL-GIS directory workbook. */
export async function sendDirectoryReport(
  quarter?: string,
): Promise<{ ok: boolean; sheet_count?: number; recipient?: string }> {
  const qs = quarter ? `?quarter=${encodeURIComponent(quarter)}` : "";
  return request(`/v1/account/directory-report/send${qs}`, { method: "POST" });
}

export interface ResendReportResult {
  ok: boolean;
  recipient: string;
  client_id: number;
  client_name: string;
}

/** Re-send the current report for a specific client.
 *  Returns {ok, recipient} on success.
 *  Throws with the Resend error message on delivery failure (502). */
export async function resendClientReport(
  clientId: number,
): Promise<ResendReportResult> {
  return request<ResendReportResult>(
    `/v1/account/clients/${clientId}/resend-report`,
    { method: "POST" },
  );
}

/** Quick-save the recipient routing mode (from the NextRunCard slider). */
export async function patchSendMode(mode: string): Promise<void> {
  await request<{ ok: boolean }>("/v1/account/reports/send-mode", {
    body: { send_mode: mode },
  });
}

export interface SampleReportResult {
  ok: boolean;
  client_name: string | null;
  sent_to: string;
  sample: boolean;
}

/** Send a sample workbook to the operator's own email only — no client is contacted. */
export async function sendSampleReport(): Promise<SampleReportResult> {
  return request<SampleReportResult>("/v1/account/send-sample-report", {
    method: "POST",
  });
}

/** Persist report-email customization. Empty-string fields clear to default. */
export async function updateEmailSettings(
  input: EmailSettingsInput,
): Promise<EmailSettings> {
  return request<EmailSettings & { ok: boolean }>(
    "/v1/account/email-settings",
    { body: input },
  );
}

/** Render a sample report email (fake client) without saving. */
export async function previewEmail(
  input: EmailSettingsInput,
): Promise<EmailPreview> {
  return request<EmailPreview & { ok: boolean }>(
    "/v1/account/email-preview",
    { body: input },
  );
}

// ─── email template studio ────────────────────────────────────────────────

export interface EmailTemplateData {
  subject_template: string;
  body_template: string;
  signoff: string;
  is_default: boolean;
  is_default_subject: boolean;
  is_default_body: boolean;
  is_default_signoff: boolean;
  from_email: string | null;
  available_tokens: string[];
  has_client_with_email: boolean;
  sample_client_email: string | null;
}

export interface EmailTemplatePreviewResult {
  subject_rendered: string;
  body_rendered: string;
  sample_client: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface EmailTemplateChatResult {
  assistant_reply: string;
  proposed_body: string;
  proposed_subject?: string | null;
}

export async function getEmailTemplate(): Promise<EmailTemplateData> {
  return request<EmailTemplateData>("/v1/account/reports/email-template");
}

export async function previewEmailTemplate(input: {
  subject_template?: string | null;
  body_template?: string | null;
  signoff?: string | null;
}): Promise<EmailTemplatePreviewResult> {
  return request<EmailTemplatePreviewResult>(
    "/v1/account/reports/email-template/preview",
    { body: input },
  );
}

export async function saveEmailTemplate(input: {
  subject_template?: string | null;
  body_template?: string | null;
}): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    "/v1/account/reports/email-template",
    { method: "PUT", body: input },
  );
}

export async function saveEmailSignoff(signoff: string | null): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    "/v1/account/reports/email-template/signoff",
    { method: "PUT", body: { signoff } },
  );
}

export async function resetEmailTemplate(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    "/v1/account/reports/email-template/reset",
    { body: {} },
  );
}

export async function testSendEmailTemplate(input: {
  subject_template?: string | null;
  body_template?: string | null;
  signoff?: string | null;
}): Promise<{ ok: boolean; sent_to: string }> {
  return request<{ ok: boolean; sent_to: string }>(
    "/v1/account/reports/email-template/test-send",
    { body: input },
  );
}

export async function chatEmailTemplate(input: {
  messages: ChatMessage[];
  current_body: string;
  current_subject?: string;
}): Promise<EmailTemplateChatResult> {
  return request<EmailTemplateChatResult>(
    "/v1/account/reports/email-template/chat",
    { body: input },
  );
}

// ─── clients ─────────────────────────────────────────────────────────────

export async function listClients(): Promise<ClientRow[]> {
  const res = await request<{ clients: ClientRow[] }>("/v1/account/clients");
  return res.clients;
}

export interface ClientCreateInput {
  name: string;
  contact_email?: string | null;
  cc_emails?: string | null;
  report_frequency?: string | null;
  notes?: string | null;
  gmp_email?: string | null;
  gmp_username?: string | null;
  gmp_autopopulate?: boolean;
  vec_email?: string | null;
  vec_username?: string | null;
  vec_autopopulate?: boolean;
  /** THE FOLD: per-client auto-send enrollment (patchable via updateClient). */
  auto_send?: boolean;
}

export async function createClient(
  input: ClientCreateInput,
): Promise<ClientRow> {
  const res = await request<{ client: ClientRow }>("/v1/account/clients", {
    body: input,
  });
  return res.client;
}

export interface AutoSendAllResult {
  ok: boolean;
  auto_send: boolean;
  clients: number;
  arrays: number;
  price_cents_per_array: number;
  estimated_quarterly_cents: number;
}

/** Turn generation-report auto-send on (or off) for EVERY active client — the
 *  one deliberate "turn my account on" action. Enabling also enrolls the tenant
 *  in the reports world; each reported array then bills $15 once per quarter. */
export async function setAutoSendAll(enabled: boolean): Promise<AutoSendAllResult> {
  return request<AutoSendAllResult>("/v1/account/clients/auto-send-all", {
    method: "POST",
    body: { enabled },
  });
}

export async function updateClient(
  id: number,
  patch: Partial<ClientCreateInput> & { active?: boolean },
): Promise<ClientRow> {
  const res = await request<{ client: ClientRow }>(
    `/v1/account/clients/${id}`,
    { method: "PATCH", body: patch },
  );
  return res.client;
}

export interface DeleteResult {
  ok: boolean;
  undo_token: string;
}

export async function deleteClient(id: number): Promise<DeleteResult> {
  return request<DeleteResult>(`/v1/account/clients/${id}`, { method: "DELETE" });
}

// ── Merge suggestions ───────────────────────────────────────────────
export interface MergeSuggestion {
  id: number;
  name: string;
  score: number;
  reasons: string[];
  has_gmp: boolean;
  has_vec: boolean;
}

export async function getMergeSuggestions(
  clientId: number,
): Promise<MergeSuggestion[]> {
  const res = await request<{ ok: true; suggestions: MergeSuggestion[] }>(
    `/v1/account/clients/${clientId}/merge-suggestions`,
  );
  return res.suggestions;
}

export interface MergeClientResult {
  dst_client: ClientRow;
  undo_token: string;
  merged_client_id: number;
}

/** Merge `srcId` INTO `dstId`. Reparents arrays, merges login fields,
 *  soft-deletes src. Returns the updated dst client plus an undo token. */
export async function mergeClientInto(
  srcId: number,
  dstId: number,
): Promise<MergeClientResult> {
  const res = await request<{
    ok: true;
    dst_client: ClientRow;
    undo_token: string;
    merged_client_id: number;
  }>(
    `/v1/account/clients/${srcId}/merge-into`,
    { method: "POST", body: { dst_client_id: dstId } },
  );
  return { dst_client: res.dst_client, undo_token: res.undo_token, merged_client_id: res.merged_client_id };
}

/** Reverse a previous merge within the 1-hour undo window. */
export async function undoMerge(token: string): Promise<void> {
  await request(`/v1/account/clients/merge-undo`, {
    method: "POST",
    body: { undo_token: token },
  });
}

export async function dismissMergeSuggestion(
  clientId: number,
  otherId: number,
): Promise<void> {
  await request(
    `/v1/account/clients/${clientId}/dismiss-merge/${otherId}`,
    { method: "POST" },
  );
}

// ── Array merge suggestions ─────────────────────────────────────────
export interface ArrayMergeSuggestion {
  id: number;
  name: string;
  score: number;
  reasons: string[];
  client_id: number | null;
  nepool_gis_id: string | null;
}

export async function getArrayMergeSuggestions(
  arrayId: number,
): Promise<ArrayMergeSuggestion[]> {
  const res = await request<{ ok: true; suggestions: ArrayMergeSuggestion[] }>(
    `/v1/account/arrays/${arrayId}/merge-suggestions`,
  );
  return res.suggestions;
}

export interface MergedArrayResult {
  id: number;
  name: string;
  client_id: number | null;
  nepool_gis_id: string | null;
  bill_offset_months: number;
  excluded: boolean;
  utility_accounts_count: number;
}

export async function mergeArrayInto(
  srcId: number,
  dstId: number,
): Promise<{ dst_array: MergedArrayResult; merged_from_id: number; reparented_utility_accounts: number }> {
  return request(
    `/v1/account/arrays/${srcId}/merge-into`,
    { method: "POST", body: { dst_array_id: dstId } },
  );
}

export async function dismissArrayMergeSuggestion(
  arrayId: number,
  otherId: number,
): Promise<void> {
  await request(
    `/v1/account/arrays/${arrayId}/dismiss-merge/${otherId}`,
    { method: "POST" },
  );
}

/** Re-read a client's GMP auto-populate freshness (does not poll GMP). */
export async function refreshCapture(id: number): Promise<ClientRow> {
  const res = await request<{ client: ClientRow }>(
    `/v1/account/clients/${id}/refresh-capture`,
    { method: "POST" },
  );
  return res.client;
}

/** Send a client's report to the operator's own email (not the client).
 *  `toEmail` must match the operator's account email — validated server-side.
 *  Optional `quarter` (e.g. "Q1-2026") selects the headline complete quarter. */
export async function sendClientReportToMe(
  clientId: number,
  toEmail: string,
  quarter?: string,
): Promise<{ ok: boolean; recipient: string }> {
  const qs = new URLSearchParams({ to: toEmail });
  if (quarter) qs.set("quarter", quarter);
  return request<{ ok: boolean; recipient: string }>(
    `/v1/account/clients/${clientId}/send-report?${qs.toString()}`,
    { method: "POST" },
  );
}

/** Most-recently-complete calendar quarters for the report picker.
 *  Values match backend `?quarter=Q1-2026` format. */
export function recentReportQuarters(count = 8): { value: string; label: string }[] {
  const now = new Date();
  let y = now.getUTCFullYear();
  // 1..4 quarter of "today", then step back one so default is COMPLETE.
  let q = Math.floor(now.getUTCMonth() / 3) + 1;
  q -= 1;
  if (q < 1) {
    q = 4;
    y -= 1;
  }
  const out: { value: string; label: string }[] = [];
  for (let i = 0; i < count; i++) {
    out.push({ value: `Q${q}-${y}`, label: `Q${q} ${y}` });
    q -= 1;
    if (q < 1) {
      q = 4;
      y -= 1;
    }
  }
  return out;
}

/** Fetch a .xlsx workbook for a client and trigger a browser download.
 *  If `quarter` is provided (e.g. 'Q1-2026'), the rolling window ends at that
 *  quarter. Omit for the current rolling window. */
export async function downloadClientReport(
  clientId: number,
  clientName: string,
  quarter?: string,
): Promise<void> {
  const token = getSession();
  const qs = quarter ? `?quarter=${encodeURIComponent(quarter)}` : "";
  const res = await fetchWithTimeout(
    `/v1/account/clients/${clientId}/report.xlsx${qs}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `Couldn't build the report (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  const safeName = clientName.replace(/[^A-Za-z0-9_.-]+/g, "_");
  const label = quarter ? quarter.replace(/[^A-Za-z0-9]/g, "-") : "latest";
  link.download = `${safeName}-${label}.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/** Download a client's RAW utility generation workbook for a quarter — a Monthly
 *  Summary (projects × the quarter's 3 months) across all the client's utilities
 *  (GMP + SmartHub co-ops) plus per-project daily meter detail. `quarter` like
 *  'Q1-2026'; omit for the latest. */
export async function downloadGeneration(
  clientId: number,
  clientName: string,
  quarter?: string,
): Promise<void> {
  const token = getSession();
  const qs = quarter ? `?quarter=${encodeURIComponent(quarter)}` : "";
  const res = await fetchWithTimeout(
    `/v1/account/clients/${clientId}/generation.xlsx${qs}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `Couldn't build the generation export (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  const safeName = clientName.replace(/[^A-Za-z0-9_.-]+/g, "_");
  const label = quarter ? quarter.replace(/[^A-Za-z0-9]/g, "-") : "latest";
  link.download = `${safeName}-${label}-generation.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/** Stream the all-time aggregated fleet report (Excel or PDF) and trigger a
 *  browser download. Session-authed (Bearer token); the server reads the DB
 *  live so the file always reflects the latest absorbed month. */
export async function downloadFleetReport(fmt: "xlsx" | "pdf"): Promise<void> {
  const token = getSession();
  const res = await fetchWithTimeout(
    `/v1/account/fleet-report?fmt=${fmt}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `Couldn't build the report (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  // Prefer the server-provided filename; fall back to a sensible default.
  let filename = `FleetReport-AllTime.${fmt}`;
  const cd = res.headers.get("Content-Disposition");
  const match = cd?.match(/filename="?([^"]+)"?/);
  if (match) filename = match[1];
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

/** Download a billing customer's OWN-format populated invoice (.xlsx) — the
 *  workbook in the customer's own uploaded layout. Only meaningful for
 *  workbook-sourced subscriptions; the backend returns 422 for manual
 *  (typed-in) customers, which callers should surface gracefully. Session-
 *  authed (Bearer token); honors the server's Content-Disposition filename and
 *  falls back to `${customerName}_invoice.xlsx`. */
export async function downloadInvoiceWorkbook(
  subId: number,
  customerName: string,
): Promise<void> {
  const token = getSession();
  const res = await fetchWithTimeout(
    `/v1/array-operator/billing/subscriptions/${subId}/preview?kind=invoice&fmt=xlsx`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `Couldn't build the invoice (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  // Prefer the server-provided filename; fall back to a sensible default.
  const safeName = customerName.replace(/[^A-Za-z0-9_.-]+/g, "_");
  let filename = `${safeName}_invoice.xlsx`;
  const cd = res.headers.get("Content-Disposition");
  const match = cd?.match(/filename=\"?([^\"]+)\"?/);
  if (match) filename = match[1];
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

export interface ProductionMonthEntry {
  month: string; // "YYYY-MM"
  mwh: number;
  by_array: { array_id: number; array_name: string; mwh: number }[];
}

export interface ProductionStats {
  last_30_days: { mwh: number; vs_prev_year_pct: number | null };
  last_12_months: { mwh: number; vs_prev_ttm_pct: number | null };
  ytd: { mwh: number };
}

export interface ProductionData {
  months: ProductionMonthEntry[];
  stats: ProductionStats;
}

export async function getClientProduction(
  clientId: number,
  months = 12,
): Promise<ProductionData> {
  const res = await request<ProductionData & { ok: boolean }>(
    `/v1/account/clients/${clientId}/production?months=${months}`,
  );
  return res;
}

// ─── quarterly progress ────────────────────────────────────────────────

export interface QuarterlyProgressArray {
  id: number;
  name: string;
}

export interface QuarterlyProgressMissingArray {
  id: number;
  name: string;
  missing_months: string[]; // e.g. ["2026-06"]
}

export interface QuarterlyProgress {
  quarter: string; // e.g. "Q2-2026"
  quarter_start: string; // ISO date
  quarter_end: string; // ISO date
  ready_arrays: QuarterlyProgressArray[];
  missing_arrays: QuarterlyProgressMissingArray[];
  total_arrays: number;
  all_ready: boolean;
}

export async function getQuarterlyProgress(
  clientId: number,
): Promise<QuarterlyProgress> {
  return request<QuarterlyProgress>(
    `/v1/account/clients/${clientId}/quarterly_progress`,
  );
}

// ─── arrays ──────────────────────────────────────────────────────────────

export async function listArrays(clientId: number): Promise<ArrayRow[]> {
  const res = await request<{ arrays: ArrayRow[] }>(
    `/v1/account/clients/${clientId}/arrays`,
  );
  return res.arrays;
}

export interface ArrayCreateInput {
  name: string;
  nepool_gis_id?: string | null;
  region?: string | null;
  bill_offset_months?: number | null;
  notes?: string | null;
  /** V2: generation source. Omit to let the backend default to 'solar'. */
  fuel_type?: string | null;
}

export async function createArray(
  clientId: number,
  input: ArrayCreateInput,
): Promise<ArrayRow> {
  const res = await request<{ array: ArrayRow }>(
    `/v1/account/clients/${clientId}/arrays`,
    { body: input },
  );
  return res.array;
}

export async function updateArray(
  clientId: number,
  arrayId: number,
  patch: Partial<ArrayCreateInput>,
): Promise<ArrayRow> {
  const res = await request<{ array: ArrayRow }>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}`,
    { method: "PATCH", body: patch },
  );
  return res.array;
}

export async function deleteArray(
  clientId: number,
  arrayId: number,
): Promise<DeleteResult> {
  return request<DeleteResult>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}`,
    { method: "DELETE" },
  );
}

export interface RestoreArrayResult {
  ok: boolean;
  array: ArrayRow;
  note?: string | null;
}

/** Restore a soft-deleted array within the 30-day grace window.
 *  Throws on 410 (purge-window-elapsed) or network errors — callers handle. */
export async function restoreArray(
  clientId: number,
  arrayId: number,
): Promise<RestoreArrayResult> {
  return request<RestoreArrayResult>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}/restore`,
    { method: "POST" },
  );
}

export interface BulkDeleteResult {
  ok: boolean;
  soft_deleted: number;
  undo_token: string;
}

export async function bulkDeleteArrays(ids: number[]): Promise<BulkDeleteResult> {
  return request<BulkDeleteResult>("/v1/account/arrays/bulk", {
    method: "DELETE",
    body: { ids },
  });
}

export async function bulkDeleteClients(ids: number[]): Promise<BulkDeleteResult> {
  return request<BulkDeleteResult>("/v1/account/clients-bulk", {
    method: "DELETE",
    body: { ids },
  });
}

export async function undoDelete(undoToken: string): Promise<{ ok: boolean; restored_arrays: number; restored_clients: number }> {
  return request("/v1/account/undo-delete", { body: { undo_token: undoToken } });
}

// ─── daily generation CSV ─────────────────────────────────────────────────

export interface DailyCsvUploadResult {
  rows_inserted: number;
  rows_updated: number;
  rows_skipped: number;
  date_range: { start: string; end: string } | null;
  source: string;
  /** "header-detected" (named columns found) or "no-header-fallback" (assumed date,kWh). */
  detected_format?: string;
}

export interface DailyCoverage {
  day_count: number;
  first_day: string | null;
  last_day: string | null;
  source_counts: Record<string, number>;
  /** Most recent ingest time; null if nothing has ever been uploaded. Lets the
   *  UI distinguish "no data yet" from "data exists but is stale". */
  last_upload_at?: string | null;
}

export async function uploadDailyCsv(
  arrayId: number,
  file: File,
): Promise<DailyCsvUploadResult> {
  const token = getSession();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const form = new FormData();
  form.append("file", file);

  const res = await fetchWithTimeout(
    `/v1/account/arrays/${arrayId}/daily-csv`,
    { method: "POST", headers, body: form },
  );

  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  return res.json() as Promise<DailyCsvUploadResult>;
}

export async function getDailyCoverage(arrayId: number): Promise<DailyCoverage> {
  return request<DailyCoverage>(`/v1/account/arrays/${arrayId}/daily-coverage`);
}

// ─── SolarEdge integration ───────────────────────────────────────────────

export interface SolarEdgeSite {
  site_id: number;
  name: string;
  address: string;
  peak_kw: number;
}

export interface SolarEdgeSetupResult {
  ok: boolean;
  /** True when the account-level key covers multiple sites — UI should show a picker. */
  needs_site_selection: boolean;
  /** Only set when needs_site_selection is true. */
  sites?: SolarEdgeSite[];
  /** Only set when needs_site_selection is false. */
  site_name?: string;
  peak_kw?: number;
  site_id?: number;
  hint?: string;
}

export interface SolarEdgePreviewResult {
  ok: boolean;
  days_pulled: number;
  sample: { day: string; kwh: number }[];
}

export async function setupSolarEdge(
  clientId: number,
  arrayId: number,
  apiKey: string,
  siteId?: number,
): Promise<SolarEdgeSetupResult> {
  return request<SolarEdgeSetupResult>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}/solaredge`,
    { body: { api_key: apiKey, ...(siteId !== undefined ? { site_id: siteId } : {}) } },
  );
}

export async function previewSolarEdge(
  clientId: number,
  arrayId: number,
): Promise<SolarEdgePreviewResult> {
  return request<SolarEdgePreviewResult>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}/solaredge/preview`,
  );
}

export async function disconnectSolarEdge(
  clientId: number,
  arrayId: number,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}/solaredge`,
    { method: "DELETE" },
  );
}

// ─── utility accounts (under an array) ───────────────────────────────────

export async function addUtilityAccount(
  clientId: number,
  arrayId: number,
  input: { provider: string; account_number: string; nickname?: string | null },
): Promise<UtilityAccount> {
  const res = await request<{ account: UtilityAccount }>(
    `/v1/account/clients/${clientId}/arrays/${arrayId}/accounts`,
    { body: input },
  );
  return res.account;
}

export async function removeUtilityAccount(
  clientId: number,
  arrayId: number,
  acctId: number,
): Promise<void> {
  await request(
    `/v1/account/clients/${clientId}/arrays/${arrayId}/accounts/${acctId}`,
    { method: "DELETE" },
  );
}

export interface Provider {
  code: string;
  label: string;
  status?: string;
  scrape_status?: string;
}

export async function listProviders(): Promise<Provider[]> {
  const res = await request<{ providers: Provider[] }>("/v1/providers", {
    noAuth: true,
  });
  return res.providers;
}

// ─── reports history ─────────────────────────────────────────────────────

export interface QuarterReport {
  quarter: string;
  year: number;
  quarter_num: number;
  status: "sent" | "ready" | "draft" | "empty";
  array_count: number;
  last_generated_at: string | null;
  last_delivered_at: string | null;
  mwh_total: number;
}

export async function getReports(quarters = 6): Promise<QuarterReport[]> {
  const res = await request<{ reports: QuarterReport[] }>(
    `/v1/account/reports?quarters=${quarters}`,
  );
  return res.reports;
}

export async function regenerateReport(
  quarter?: string,
  clientId?: number,
): Promise<{ status: string; generated_at: string }> {
  const body: { quarter?: string; client_id?: number } = {};
  if (quarter) body.quarter = quarter;
  if (clientId !== undefined) body.client_id = clientId;
  return request<{ status: string; generated_at: string }>(
    "/v1/account/regenerate",
    { method: "POST", body },
  );
}

/** Preview of the next scheduled delivery run. */
export interface NextRunPreview {
  next_run_date: string;
  days_until: number;
  frequency: string;
  array_count: number;
  mwh_preview: number;
  rec_preview: number;
  client_count: number;
}

export async function getNextRun(): Promise<NextRunPreview> {
  return request<NextRunPreview>("/v1/account/reports/next-run");
}

// ─── billing trends (CONTRACT 1 — macro multi-year trends) ─────────────────
// Consumes GET /v1/array-operator/billing/subscriptions/{id}/trends.
// The backend PR is built in parallel and may not be merged when this ships,
// so every field is read DEFENSIVELY: missing scalars become null, absent
// collections become empty. Callers can rely on the normalized shape below.

/** One calendar month of a year's series. `savings` is USD (may be absent). */
export interface TrendMonthPoint {
  month: number; // 1–12
  kwh: number;
  savings: number | null;
}

/** Per-calendar-month seasonal comparison across the years present. */
export interface SeasonalYoYEntry {
  month: number; // 1–12
  label: string; // "Jan"
  /** year (as string key) → kWh for that month. */
  by_year: Record<string, number>;
  /** % change of the latest year vs the immediately prior year for this month.
   *  Null when there is no prior year to compare against. */
  latest_delta_pct: number | null;
}

export interface BillingTrends {
  customer_name: string | null;
  years: number[];
  /** year (as string key) → that year's monthly points (only months with data). */
  monthly_by_year: Record<string, TrendMonthPoint[]>;
  seasonal_yoy: SeasonalYoYEntry[];
  ttm_kwh: number | null;
  ttm_savings: number | null;
  lifetime_kwh: number | null;
  summary_note: string | null;
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Coerce an arbitrary backend payload into a safe BillingTrends. Tolerates
 *  missing fields, wrong-typed collections, and partial month entries — a thin
 *  or not-yet-deployed backend must NEVER throw here, just yield empty data. */
function normalizeTrends(raw: unknown): BillingTrends {
  const r = (raw ?? {}) as Record<string, unknown>;

  const years = Array.isArray(r.years)
    ? (r.years.filter((y) => typeof y === "number") as number[])
    : [];

  const monthly_by_year: Record<string, TrendMonthPoint[]> = {};
  const rawMonthly = (r.monthly_by_year ?? {}) as Record<string, unknown>;
  if (rawMonthly && typeof rawMonthly === "object") {
    for (const [year, list] of Object.entries(rawMonthly)) {
      if (!Array.isArray(list)) continue;
      const points: TrendMonthPoint[] = [];
      for (const entry of list) {
        const e = (entry ?? {}) as Record<string, unknown>;
        const month = num(e.month);
        const kwh = num(e.kwh);
        if (month === null || kwh === null) continue;
        points.push({ month, kwh, savings: num(e.savings) });
      }
      points.sort((a, b) => a.month - b.month);
      monthly_by_year[year] = points;
    }
  }

  const seasonal_yoy: SeasonalYoYEntry[] = [];
  if (Array.isArray(r.seasonal_yoy)) {
    for (const entry of r.seasonal_yoy) {
      const e = (entry ?? {}) as Record<string, unknown>;
      const month = num(e.month);
      if (month === null) continue;
      const by_year: Record<string, number> = {};
      const rawBy = (e.by_year ?? {}) as Record<string, unknown>;
      if (rawBy && typeof rawBy === "object") {
        for (const [year, val] of Object.entries(rawBy)) {
          const n = num(val);
          if (n !== null) by_year[year] = n;
        }
      }
      seasonal_yoy.push({
        month,
        label: typeof e.label === "string" ? e.label : MONTH_ABBR[month - 1] ?? String(month),
        by_year,
        latest_delta_pct: num(e.latest_delta_pct),
      });
    }
    seasonal_yoy.sort((a, b) => a.month - b.month);
  }

  return {
    customer_name: typeof r.customer_name === "string" ? r.customer_name : null,
    years: [...years].sort((a, b) => a - b),
    monthly_by_year,
    seasonal_yoy,
    ttm_kwh: num(r.ttm_kwh),
    ttm_savings: num(r.ttm_savings),
    lifetime_kwh: num(r.lifetime_kwh),
    summary_note: typeof r.summary_note === "string" ? r.summary_note : null,
  };
}

const MONTH_ABBR = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** Fetch multi-year billing trends for a subscription. The reports UI has no
 *  separate subscription list, and the endpoint is customer-scoped (it returns
 *  `customer_name`), so callers pass the client/customer id as the subscription
 *  identifier — see the "View trends" link in QuarterCard. Accepts string|number
 *  so a differently-keyed backend still composes. */
export async function getBillingTrends(
  subscriptionId: string | number,
): Promise<BillingTrends> {
  const raw = await request<unknown>(
    `/v1/array-operator/billing/subscriptions/${encodeURIComponent(
      String(subscriptionId),
    )}/trends`,
  );
  return normalizeTrends(raw);
}

// ─── billing subscriptions (Array Operator customers) ──────────────────────
// A subscription = ONE customer who gets a % allocation of an array's output,
// invoiced per period. Two creation paths on the backend:
//   * upload an .xlsx billing workbook, or
//   * MANUAL — type the customer in (name, array, allocation %, email, cadence,
//     delivery + send mode). This client only exercises the manual path; the
//     upload path lives in the existing workbook-upload flow.

export interface BillingSubscription {
  id: number;
  customer_name: string;
  client_id: number | null;
  array_id: number | null;
  allocation_pct: number | null;
  billing_model: string;
  cadence: string;
  delivery_mode: string;
  send_mode: string;
  client_email: string | null;
  cc_emails: string | null;
  operator_email: string | null;
  formats: string[];
  include_summary: boolean;
  enabled: boolean;
  source_filename: string | null;
  last_sent_at: string | null;
  next_send_at: string | null;
  last_invoice_number: string | null;
}

export async function listBillingSubscriptions(): Promise<BillingSubscription[]> {
  const res = await request<{ ok: boolean; subscriptions: BillingSubscription[] }>(
    "/v1/array-operator/billing/subscriptions",
  );
  return res.subscriptions ?? [];
}

export interface ManualCustomerInput {
  customer_name: string;
  array_id: number;
  /** Fraction in (0, 1] — e.g. 0.25 for 25%. */
  allocation_pct: number;
  client_email?: string | null;
  cadence?: "monthly" | "quarterly";
  delivery_mode?: "approval" | "auto";
  send_mode?: "to_me" | "to_client" | "to_both";
  cc_emails?: string | null;
}

/** Create a customer with NO workbook — the manual-input path. POSTs
 *  multipart/form-data WITHOUT a file so the backend takes its typed branch. */
export async function createManualSubscription(
  input: ManualCustomerInput,
): Promise<BillingSubscription> {
  const token = getSession();
  const fd = new FormData();
  fd.append("customer_name", input.customer_name);
  fd.append("array_id", String(input.array_id));
  fd.append("allocation_pct", String(input.allocation_pct));
  if (input.client_email) fd.append("client_email", input.client_email);
  if (input.cc_emails) fd.append("cc_emails", input.cc_emails);
  fd.append("cadence", input.cadence ?? "monthly");
  fd.append("delivery_mode", input.delivery_mode ?? "approval");
  fd.append("send_mode", input.send_mode ?? "to_me");

  const res = await fetchWithTimeout(
    "/v1/array-operator/billing/subscriptions",
    {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: fd,
    },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  const body = (await res.json()) as { subscription: BillingSubscription };
  return body.subscription;
}

/** Every array the operator owns, flattened across their clients, for the
 *  manual-customer array picker. Each entry carries the owning client id so the
 *  picker can group/label. */
export interface FlatArray {
  id: number;
  name: string;
  client_id: number;
  client_name: string;
}

export async function listAllArrays(): Promise<FlatArray[]> {
  const clients = await listClients();
  const active = clients.filter((c) => c.active);
  const lists = await Promise.all(
    active.map(async (c) => {
      try {
        const arrays = await listArrays(c.id);
        return arrays
          .filter((a) => !a.deleted_at && !a.excluded)
          .map((a) => ({
            id: a.id,
            name: a.name,
            client_id: c.id,
            client_name: c.name,
          }));
      } catch {
        return [] as FlatArray[];
      }
    }),
  );
  return lists.flat();
}

/** Patch a billing subscription. The redesigned Reports tab uses this for the
 *  inline allocation-% edit (allocation_pct is a fraction in (0, 1]) and for
 *  reassigning the array. Any other editable field is accepted too. */
export interface SubscriptionPatchInput {
  customer_name?: string;
  cadence?: "monthly" | "quarterly";
  delivery_mode?: "approval" | "auto";
  send_mode?: "to_me" | "to_client" | "to_both";
  client_email?: string | null;
  cc_emails?: string | null;
  operator_email?: string | null;
  enabled?: boolean;
  /** Fraction in (0, 1] — e.g. 0.95 for 95%. */
  allocation_pct?: number;
  array_id?: number;
}

export async function patchSubscription(
  id: number,
  patch: SubscriptionPatchInput,
): Promise<BillingSubscription> {
  const res = await request<{ ok: boolean; subscription: BillingSubscription }>(
    `/v1/array-operator/billing/subscriptions/${id}`,
    { method: "PATCH", body: patch },
  );
  return res.subscription;
}

// ─── billing drafts (the approval inbox / per-period review) ────────────────
// A draft = ONE customer's invoice for the current billing period, pending the
// operator's "Approve & send". The redesigned Reports run-table surfaces these.

export interface ReportDraft {
  id: number;
  subscription_id: number;
  customer_name: string;
  status: "pending" | "sent" | "dismissed";
  period_label: string | null;
  array_total_kwh: number | null;
  allocation_pct: number | null;
  customer_kwh: number | null;
  amount_usd: number | null;
  invoice_number: string | null;
  has_gmp_pdf: boolean;
  gmp_filename: string | null;
  note: string | null;
  created_at: string | null;
  sent_at: string | null;
}

/** A draft-less, eager preview of a subscription's billing math for the latest
 *  period. Powers the run-table rows so every customer shows real, auditable
 *  numbers (generation × allocation = kWh × rate = $) without generating a
 *  draft. `has_data` is false when the array has no generation yet — in that
 *  case the kWh/amount fields are null and the UI shows a muted note instead of
 *  a fabricated number. */
export interface SubscriptionPreview {
  subscription_id: number;
  source: string;
  has_data: boolean;
  allocation_pct: number | null;
  array_total_kwh: number | null;
  customer_kwh: number | null;
  amount_usd: number | null;
  rate: number | null;
  period_start: string | null;
  period_end: string | null;
}

export async function getSubscriptionPreview(
  subId: number,
): Promise<SubscriptionPreview> {
  return request<SubscriptionPreview>(
    `/v1/array-operator/billing/subscriptions/${subId}/preview-math`,
  );
}

export async function listDrafts(
  status: "pending" | "sent" | "dismissed" | "all" = "pending",
): Promise<ReportDraft[]> {
  const res = await request<{ drafts: ReportDraft[] }>(
    `/v1/array-operator/billing/drafts?status=${status}`,
  );
  return res.drafts ?? [];
}

/** Build (or fetch the idempotent) pending draft for a subscription's latest
 *  billing period, then open the review drawer with it. */
export async function generateDraft(subId: number): Promise<ReportDraft> {
  const res = await request<{ ok: boolean; draft: ReportDraft }>(
    `/v1/array-operator/billing/subscriptions/${subId}/draft`,
    { method: "POST", body: {} },
  );
  return res.draft;
}

/** Edit a draft before sending (currently the operator note). */
export async function patchDraft(
  draftId: number,
  patch: { note?: string },
): Promise<ReportDraft> {
  const res = await request<{ ok: boolean; draft: ReportDraft }>(
    `/v1/array-operator/billing/drafts/${draftId}`,
    { method: "PATCH", body: patch },
  );
  return res.draft;
}

/** Approve & send a draft — the single human gate in front of delivery. */
export async function approveDraft(draftId: number): Promise<ReportDraft> {
  const res = await request<{ ok: boolean; draft: ReportDraft }>(
    `/v1/array-operator/billing/drafts/${draftId}/approve`,
    { method: "POST", body: {} },
  );
  return res.draft;
}

/** Discard a draft without sending. */
export async function dismissDraft(draftId: number): Promise<void> {
  await request(`/v1/array-operator/billing/drafts/${draftId}/dismiss`, {
    method: "POST",
    body: {},
  });
}

/** Attach the period's GMP utility-invoice PDF to a draft (multipart upload). */
export async function attachGmpInvoice(
  draftId: number,
  file: File,
): Promise<ReportDraft> {
  const token = getSession();
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetchWithTimeout(
    `/v1/array-operator/billing/drafts/${draftId}/gmp-invoice`,
    {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: fd,
    },
  );
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  const body = (await res.json()) as { draft: ReportDraft };
  return body.draft;
}

// ─── data sponge (energy history) ──────────────────────────────────────────
// The owner's FULL utility energy record, absorbed at onboarding. Two endpoints:
//   GET /v1/account/sponge          → live progress of the absorb (progress bar)
//   GET /v1/account/energy-history  → the absorbed multi-year record + summary
// Read defensively so a thin / not-yet-deployed backend never throws.

export interface SpongeStatus {
  status: "idle" | "running" | "done" | "error";
  accounts_total: number;
  accounts_done: number;
  bills_absorbed: number;
  years_covered: number | null;
  pct: number;
  message: string | null;
  error?: string | null;
}

export interface EnergyPeriod {
  period_start: string | null;
  period_end: string | null;
  bill_date: string | null;
  billing_days: number | null;
  kwh_generated: number | null;
  kwh_consumed: number | null;
  kwh_sent_to_grid: number | null;
  kwh_gross_generated: number | null;
  is_net_metered: boolean | null;
  total_cost: number | null;
  net_credit: number | null;
  avg_rate_cents_kwh: number | null;
  supplier: string | null;
}

export interface EnergyHistory {
  summary: {
    bills: number;
    years_covered: number | null;
    total_kwh_generated: number;
    total_kwh_consumed: number;
  };
  periods: EnergyPeriod[];
}

export async function getSpongeStatus(): Promise<SpongeStatus> {
  const raw = (await request<unknown>("/v1/account/sponge")) as Record<string, unknown>;
  const r = raw ?? {};
  const s = typeof r.status === "string" ? r.status : "idle";
  return {
    status: (["idle", "running", "done", "error"].includes(s) ? s : "idle") as SpongeStatus["status"],
    accounts_total: num(r.accounts_total) ?? 0,
    accounts_done: num(r.accounts_done) ?? 0,
    bills_absorbed: num(r.bills_absorbed) ?? 0,
    years_covered: num(r.years_covered),
    pct: num(r.pct) ?? 0,
    message: typeof r.message === "string" ? r.message : null,
    error: typeof r.error === "string" ? r.error : null,
  };
}

export async function getEnergyHistory(): Promise<EnergyHistory> {
  const raw = (await request<unknown>("/v1/account/energy-history")) as Record<string, unknown>;
  const r = raw ?? {};
  const rs = (r.summary ?? {}) as Record<string, unknown>;
  const periodsRaw = Array.isArray(r.periods) ? r.periods : [];
  const periods: EnergyPeriod[] = periodsRaw.map((p) => {
    const e = (p ?? {}) as Record<string, unknown>;
    const str = (k: string) => (typeof e[k] === "string" ? (e[k] as string) : null);
    const bool = (k: string) => (typeof e[k] === "boolean" ? (e[k] as boolean) : null);
    return {
      period_start: str("period_start"),
      period_end: str("period_end"),
      bill_date: str("bill_date"),
      billing_days: num(e.billing_days),
      kwh_generated: num(e.kwh_generated),
      kwh_consumed: num(e.kwh_consumed),
      kwh_sent_to_grid: num(e.kwh_sent_to_grid),
      kwh_gross_generated: num(e.kwh_gross_generated),
      is_net_metered: bool("is_net_metered"),
      total_cost: num(e.total_cost),
      net_credit: num(e.net_credit),
      avg_rate_cents_kwh: num(e.avg_rate_cents_kwh),
      supplier: str("supplier"),
    };
  });
  return {
    summary: {
      bills: num(rs.bills) ?? 0,
      years_covered: num(rs.years_covered),
      total_kwh_generated: num(rs.total_kwh_generated) ?? 0,
      total_kwh_consumed: num(rs.total_kwh_consumed) ?? 0,
    },
    periods,
  };
}

// ─── spreadsheet ingest (V4) ─────────────────────────────────────────────

/** Per-row provenance from the server — how data was extracted and whether
 *  it collides with existing records. Added in the smarter-import update. */
export interface IngestRowProvenance {
  source: "gmcs" | "llm" | "heuristic";
  /** LLM confidence 0–1, or null for non-LLM sources. */
  confidence: number | null;
  /** Set when the row's operator_name matches an existing Client. */
  client_match: {
    client_id: number;
    client_name: string;
    match_kind: "exact" | "fuzzy" | "filename";
  } | null;
  /** Set when the row's nepool_gis_id matches an existing Array. */
  nepool_collision: {
    existing_array_id: number;
    existing_array_name: string;
    existing_client_name: string;
  } | null;
}

/** Top-level soft warning returned alongside the preview. */
export interface IngestWarning {
  kind: "empty_file" | "low_confidence_rows" | "client_collision" | "nepool_collision";
  count: number;
  message: string;
}

/** One extracted array row from a roster upload — every field user-editable. */
export interface IngestRow {
  operator_name: string | null;
  array_name: string | null;
  nepool_gis_id: string | null;
  gmp_account_number: string | null;
  notes: string | null;
  /** Set by the server during preview if the name matches an existing record.
   *  "client" = operator name matches; "array" = array name matches; "both" = both. */
  collision?: "client" | "array" | "both" | null;
  /** Per-row provenance — added by server in preview response. */
  provenance?: IngestRowProvenance | null;
  /** How to handle this row on commit when there's a NEPOOL collision.
   *  "skip" = skip entirely; "overwrite"/"new" = proceed (default "new"). */
  collision_action?: "skip" | "overwrite" | "new" | null;
}

export interface IngestPreview {
  source: "llm" | "heuristic" | "gmcs_shape";
  count: number;
  arrays: IngestRow[];
  /** Number of distinct utility logins detected in the hierarchical extraction. */
  imported_logins: number;
  /** Number of distinct clients detected in the hierarchical extraction. */
  imported_clients: number;
  /** Top-level soft warnings (empty file, low confidence, collisions). */
  warnings: IngestWarning[];
}

export interface IngestCommitResult {
  clients_created: number;
  arrays_created: number;
  accounts_created: number;
  /** Rows skipped because the user chose collision_action="skip". */
  skipped_count?: number;
}

/** Upload a spreadsheet and get back parsed rows (nothing is saved yet). */
export async function ingestPreview(
  file: File,
  signal?: AbortSignal,
): Promise<IngestPreview> {
  const form = new FormData();
  form.append("file", file);

  const headers: Record<string, string> = {};
  const token = getSession();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // NOTE: do NOT set Content-Type — the browser adds the multipart boundary.

  // Longer timeout (120s): the server-side AI parse of a roster can be slow.
  const res = await fetchWithTimeout(
    "/v1/ingest/preview",
    { method: "POST", headers, body: form, signal },
    120_000,
  );

  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  return (await res.json()) as IngestPreview;
}

/** Commit the (user-confirmed, possibly edited) rows. */
export async function ingestCommit(
  arrays: IngestRow[],
  forceClientId?: number,
): Promise<IngestCommitResult> {
  // When forceClientId is set, every row is pinned to that Client on the
  // backend — operator_name is ignored. Used by the per-client "Import
  // arrays into this client" button so the user doesn't have to scrub
  // the operator_name column to match the target.
  const body: { arrays: IngestRow[]; force_client_id?: number } = { arrays };
  if (forceClientId !== undefined) body.force_client_id = forceClientId;
  return request<IngestCommitResult>("/v1/ingest/commit", { body });
}

// ─── NEPOOL ID assignment ─────────────────────────────────────────────────

export interface NepoolStats {
  arrays_missing_nepool: number;
}

export interface NepoolProposal {
  extracted_name: string;
  extracted_nepool_gis_id: string;
  match: {
    array_id: number;
    array_name: string;
    current_nepool_gis_id: string | null;
    confidence: number;
    would_overwrite: boolean;
  };
}

export interface NepoolUnmatchedPair {
  extracted_name: string;
  extracted_nepool_gis_id: string;
}

export interface NepoolAvailableArray {
  array_id: number;
  array_name: string;
  client_name: string | null;
}

export interface NepoolPreviewResult {
  ok: boolean;
  source: "gmcs_shape" | "llm" | "heuristic";
  pairs_extracted: number;
  matches_proposed: number;
  unmatched: number;
  skipped_overwrites: number;
  proposals: NepoolProposal[];
  unmatched_pairs: NepoolUnmatchedPair[];
  available_arrays: NepoolAvailableArray[];
}

export interface NepoolCommitResult {
  ok: boolean;
  updated: number;
  errors: Array<{ array_id: number; reason: string }>;
}

export function getNepoolStats(): Promise<NepoolStats> {
  return request<NepoolStats>("/v1/account/nepool/stats");
}

export async function nepoolPreview(
  file: File,
  signal?: AbortSignal,
  clientId?: number,
): Promise<NepoolPreviewResult> {
  const form = new FormData();
  form.append("file", file);

  const headers: Record<string, string> = {};
  const token = getSession();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const url =
    clientId !== undefined
      ? `/v1/account/nepool/preview?client_id=${clientId}`
      : "/v1/account/nepool/preview";

  const res = await fetchWithTimeout(
    url,
    { method: "POST", headers, body: form, signal },
    120_000,
  );

  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  return (await res.json()) as NepoolPreviewResult;
}

// ─── sandbox canvas ────────────────────────────────────────────────────────

export interface CanvasAccountData {
  id: number;
  provider: string;
  account_number: string;
  customer_number?: string | null;
  service_address: Record<string, unknown> | null;
  canvas_x: number | null;
  canvas_y: number | null;
  canvas_pinned: boolean;
  array_id: number | null;
  array_name: string | null;
  nepool_gis_id: string | null;
  /** V2: generation source of the linked array (solar|wind|hydro|digester|
   *  storage). Null/absent reads as solar. */
  fuel_type?: string | null;
  /** Original client this account belonged to before being moved in the
   *  sandbox. NULL while the account is still at its original home. */
  login_origin_client_id?: number | null;
  /** ISO timestamp when the array was soft-deleted. Non-null means the array
   *  is in the 30-day purge grace window; the sandbox renders it as a ghost. */
  array_deleted_at?: string | null;
}

export interface CanvasClientData {
  id: number;
  name: string;
  contact_email?: string | null;
  canvas_x: number | null;
  canvas_y: number | null;
  canvas_pinned: boolean;
  accounts: CanvasAccountData[];
  /** Per-utility login credential (email or username) the operator gave us. */
  logins?: { GMP?: string | null; VEC?: string | null; WEC?: string | null };
}

export interface CanvasResponse {
  clients: CanvasClientData[];
  unclassified: CanvasAccountData[];
  /** Lookup table of clients referenced by login_origin_client_id, including
   *  soft-deleted ones, so the sandbox can render "from <name>" labels even
   *  when the origin client has since been removed. */
  clients_index?: Record<number, {
    id: number;
    name: string;
    deleted: boolean;
    logins: { GMP?: string | null; VEC?: string | null; WEC?: string | null };
  }>;
}

export async function getCanvasData(): Promise<CanvasResponse> {
  return request<CanvasResponse>("/v1/sandbox/canvas");
}

export interface CanvasPositionUpdate {
  node_type: "client" | "account";
  node_id: number;
  x: number;
  y: number;
}

export async function patchCanvasPositions(
  updates: CanvasPositionUpdate[],
): Promise<void> {
  if (updates.length === 0) return;
  await request("/v1/sandbox/positions", { method: "PATCH", body: updates });
}

/** Pin/star a client. Pinned clients sort to top and show a gold star. */
export async function pinClient(client_id: number, pinned: boolean): Promise<{ ok: true; client_id: number; pinned: boolean }> {
  return request("/v1/sandbox/client/pin", {
    body: { client_id, pinned },
  });
}

// ── Dev-only sandbox helpers (gated server-side by SO_DEV_ENABLED) ──────────

export interface DevStatus {
  enabled: boolean;
  tenant_id: string;
  dev_clients: number;
  dev_prefix: string;
}

export async function devStatus(): Promise<DevStatus> {
  return request("/v1/dev/status", { method: "GET" });
}

export async function devSeedClients(count = 3): Promise<{ ok: true; created: { id: number; name: string }[] }> {
  return request("/v1/dev/seed/clients", { body: { count } });
}

export async function devSeedLogin(client_id: number, utility: "GMP" | "VEC" | "WEC", arrays = 3): Promise<{ ok: true; client_id: number; customer_number: string; arrays: { id: number; name: string }[]; accounts: { id: number; account_number: string; customer_number: string; array_id: number }[] }> {
  return request("/v1/dev/seed/login", { body: { client_id, utility, arrays } });
}

export async function devSeedUnclassified(count = 2, utility: "GMP" | "VEC" | "WEC" = "GMP"): Promise<{ ok: true; created: { id: number; account_number: string }[] }> {
  return request("/v1/dev/seed/unclassified", { body: { count, utility } });
}

export async function devWipe(): Promise<{ ok: true; clients_removed: number; arrays_removed: number; accounts_removed: number }> {
  return request("/v1/dev/wipe", { body: {} });
}

/** Move a utility account to a different client (or unclassify when client_id is null).
 *  Backend reuses an existing solo holder array or creates a new one under the target. */
export async function reassignAccount(
  account_id: number,
  client_id: number | null,
): Promise<{ ok: true; account_id: number; client_id: number | null; array_id: number | null }> {
  return request("/v1/sandbox/account/reassign", {
    body: { account_id, client_id },
  });
}

/** Move an array to a different client (or unclassify when client_id is null).
 *  Re-points Array.client_id only; UtilityAccount.array_id links are unchanged.
 *  Sub-meter arrays (multiple accounts sharing one Array) move together. */
export async function reassignArray(
  array_id: number,
  client_id: number | null,
): Promise<{ ok: true; array_id: number; client_id: number | null; prior_client_id: number | null }> {
  return request("/v1/sandbox/array/reassign", {
    body: { array_id, client_id },
  });
}

// ─── capture timeline (dev-only) ─────────────────────────────────────────

export interface CaptureEventRow {
  id: number;
  stage: string;
  decision: string | null;
  payload_excerpt: Record<string, unknown> | null;
  duration_ms: number | null;
  created_at: string;
}

export interface CaptureGroup {
  capture_id: string;
  started_at: string;
  ended_at: string;
  stage_count: number;
  arrays_created: number;
  total_ms: number;
  has_error: boolean;
  client_hint: string | null;
  events: CaptureEventRow[];
}

export interface CaptureDetail {
  ok: boolean;
  capture_id: string;
  started_at: string;
  ended_at: string;
  total_ms: number;
  has_error: boolean;
  client_hint: string | null;
  events: CaptureEventRow[];
}

export async function listCaptures(
  limit = 50,
  since?: string,
): Promise<CaptureGroup[]> {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (since) qs.set("since", since);
  const res = await request<{ ok: boolean; captures: CaptureGroup[] }>(
    `/v1/dev/captures?${qs}`,
  );
  return res.captures;
}

export async function getCapture(captureId: string): Promise<CaptureDetail> {
  return request<CaptureDetail>(`/v1/dev/captures/${encodeURIComponent(captureId)}`);
}

// ──────────────────────────────────────────────────────────────────────────

export async function nepoolCommit(
  assignments: Array<{ array_id: number; nepool_gis_id: string }>,
): Promise<NepoolCommitResult> {
  return request<NepoolCommitResult>("/v1/account/nepool/commit", {
    body: { assignments },
  });
}

// ─── verify accuracy ──────────────────────────────────────────────────────

export interface VerificationCheck {
  id: number;
  tenant_id: string;
  client_id: number;
  array_id: number | null;
  uploaded_filename: string;
  uploaded_mime: string;
  period_label: string;
  status: "pending" | "confirmed" | "flagged";
  operator_note: string | null;
  created_at: string;
  resolved_at: string | null;
}

export async function uploadVerification(
  clientId: number,
  periodLabel: string,
  file: File,
  arrayId?: number,
): Promise<VerificationCheck> {
  const token = getSession();
  const fd = new FormData();
  fd.append("file", file);
  fd.append("client_id", String(clientId));
  fd.append("period_label", periodLabel);
  if (arrayId !== undefined) fd.append("array_id", String(arrayId));

  const res = await fetchWithTimeout("/v1/verification/upload", {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: fd,
  });
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function listVerifications(
  clientId: number,
): Promise<VerificationCheck[]> {
  const res = await request<{ checks: VerificationCheck[] }>(
    `/v1/verification?client_id=${clientId}`,
  );
  return res.checks;
}

export async function resolveVerification(
  id: number,
  status: "confirmed" | "flagged",
  note?: string,
): Promise<VerificationCheck> {
  return request<VerificationCheck>(`/v1/verification/${id}/resolve`, {
    method: "POST",
    body: { status, ...(note ? { note } : {}) },
  });
}

export async function fetchVerificationUploadedFile(id: number): Promise<Blob> {
  const token = getSession();
  const res = await fetchWithTimeout(`/v1/verification/${id}/uploaded-file`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  return res.blob();
}

export async function fetchVerificationSoWorkbook(
  id: number,
): Promise<ArrayBuffer> {
  const token = getSession();
  const res = await fetchWithTimeout(`/v1/verification/${id}/so-workbook`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (res.status === 401) {
    notifyUnauthorizedOnce();
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let msg = `SO workbook unavailable (${res.status})`;
    try {
      msg = (await res.json()).detail || msg;
    } catch {
      /* ignore */
    }
    throw new Error(msg);
  }
  return res.arrayBuffer();
}

// ─── array owners (EnergyAgent dashboard) ──────────────────────────────────
// See docs/plans/ARRAY_OWNERS_API_CONTRACT.md and lib/arrayOwners.ts.

/** Live value/health overview across every array the tenant owns. Polled by
 *  the Arrays screen; cheap enough to hit every 60s. */
export async function arrayOwnersOverview(): Promise<ArrayOwnersOverview> {
  return request<ArrayOwnersOverview>("/v1/array-owners/overview");
}

/** Connect a SolarEdge inverter to an array. The backend validates the key
 *  with a live SolarEdge overview call before saving — a bad key/site comes
 *  back as a 400 (surfaced here as a thrown Error with the server's detail). */
export async function connectSolarEdge(
  arrayId: number,
  apiKey: string,
  siteId: number,
): Promise<ConnectSolarEdgeResult> {
  return request<ConnectSolarEdgeResult>(
    `/v1/array-owners/arrays/${arrayId}/solaredge`,
    { body: { api_key: apiKey, site_id: siteId } },
  );
}

/** Preview every SolarEdge site an ACCOUNT-LEVEL key can read. Saves nothing —
 *  the modal shows the sites as checkboxes before the operator commits. A
 *  site-level key (403) or bad key (401) throws with the server's guidance. */
export async function discoverSolarEdge(
  apiKey: string,
): Promise<SolarEdgeDiscoverResult> {
  return request<SolarEdgeDiscoverResult>(
    "/v1/array-owners/solaredge/discover",
    { body: { api_key: apiKey } },
  );
}

/** Attach every (or a chosen subset of) SolarEdge site on an account-level key
 *  to the tenant's arrays in one shot. Idempotent — re-running updates the same
 *  arrays. Returns {connected, created, matched} so the UI can celebrate. */
export async function connectSolarEdgeAccount(
  apiKey: string,
  siteIds?: number[],
): Promise<ConnectAccountResult> {
  return request<ConnectAccountResult>(
    "/v1/array-owners/solaredge/connect-account",
    {
      body: {
        api_key: apiKey,
        ...(siteIds !== undefined ? { site_ids: siteIds } : {}),
      },
    },
  );
}

// ─── portal access roster (v1.9.112 multi-login vault) ─────────────────────

/** One (client, portal-identity) row from GET /v1/portal-access. */
export interface PortalAccessRow {
  client_id: number;
  client: string;
  provider: string | null;
  login_username: string | null;
  /** automated | saved_pending | failing | disabled | login_missing | no_portal_identity */
  status: string;
  last_ok_at: string | null;
  last_sync_at: string | null;
  enabled: boolean | null;
  fails: number;
}

export interface PortalAccessUnassigned {
  provider: string;
  username: string;
  status: string;
  last_ok_at: string | null;
  enabled: boolean;
  fails: number;
}

export interface PortalAccess {
  extension_alive: boolean;
  extension_last_seen: string | null;
  clients: PortalAccessRow[];
  unassigned_logins: PortalAccessUnassigned[];
}

/** Per-client portal automation roster: which client logins are saved in the
 *  extension vault (hands-off), failing (password changed), or still to be
 *  collected. Status metadata only — passwords never leave the extension. */
export async function getPortalAccess(): Promise<PortalAccess> {
  return request<PortalAccess>("/v1/portal-access");
}
