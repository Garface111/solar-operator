// Dashboard API client + session handling.
//
// The SPA is served same-origin (solaroperator.org/accounts, Netlify-proxied to
// the FastAPI /app/ mount; /v1/* is proxied too), so every call is a
// relative-path fetch. The session token lives in localStorage under
// 'so_session' and is sent as `Authorization: Bearer <token>` on /v1/account/*.
//
// On a 401 we clear the session and broadcast a window event so the app shell
// can bounce to the login screen from anywhere.

const SESSION_KEY = "so_session";
export const UNAUTHORIZED_EVENT = "so-unauthorized";

export function getSession(): string | null {
  return localStorage.getItem(SESSION_KEY);
}

export function setSession(token: string): void {
  localStorage.setItem(SESSION_KEY, token);
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}

/** Raised on a 401 so callers can distinguish auth failures from other errors. */
export class UnauthorizedError extends Error {
  constructor() {
    super("Session expired — sign in again");
    this.name = "UnauthorizedError";
  }
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail))
      return body.detail.map((d: any) => d.msg).join("; ");
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
    clearSession();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  // Some endpoints (DELETE) may return an empty body.
  const text = await res.text();
  return (text ? JSON.parse(text) : {}) as T;
}

// ─── auth ──────────────────────────────────────────────────────────────────

export async function requestLoginLink(email: string, persist = true): Promise<void> {
  await request("/v1/auth/request", { body: { email, persist }, noAuth: true });
}

export async function verifyLoginToken(token: string): Promise<string> {
  const res = await request<{ session_token: string }>("/v1/auth/verify", {
    body: { token },
    noAuth: true,
  });
  return res.session_token;
}

// ─── types ───────────────────────────────────────────────────────────────

export interface Account {
  tenant_id: string;
  tenant_key: string | null;
  name: string | null;
  email: string | null;
  plan: string | null;
  active: boolean;
  subscription_status: string | null;
  report_frequency: string | null;
  cc_on_reports: boolean;
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
  created_at: string | null;
  trial_ends_at: string | null;
  accounts_count: number;
  bills_count: number;
  clients_count: number;
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
  nickname: string | null;
}

export interface ArrayRow {
  id: number;
  name: string;
  nepool_gis_id: string | null;
  region: string | null;
  bill_offset_months: number | null;
  notes: string | null;
  excluded: boolean;
  accounts: UtilityAccount[];
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
   *  client" placeholder. Cleared the moment the operator renames it,
   *  pastes a utility-login email, or arrays land via autopopulate. */
  is_placeholder?: boolean;
}

// ─── account ───────────────────────────────────────────────────────────────

export function getAccount(): Promise<Account> {
  return request<Account>("/v1/account");
}

export async function updateAccountEmail(email: string): Promise<string> {
  const res = await request<{ email: string }>("/v1/account/email", {
    body: { email },
  });
  return res.email;
}

export async function updateAccountFrequency(
  frequency: string,
): Promise<string> {
  const res = await request<{ frequency: string }>("/v1/account/frequency", {
    body: { frequency },
  });
  return res.frequency;
}

export async function getBillingPortalUrl(): Promise<string> {
  const res = await request<{ url: string }>("/v1/account/billing-portal");
  return res.url;
}

export interface BillingSummary {
  billable_arrays: number;
  price_cents: number;
  total_cents: number;
  currency: string;
}

/** What the tenant is billed for: array count (the Stripe quantity) × per-array
 *  price. Lets the Account tab show the real monthly figure. */
export async function getBillingSummary(): Promise<BillingSummary> {
  return request<BillingSummary>("/v1/account/billing-summary");
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
 *  Returns the per-client outcome so the UI can tell the truth about partial
 *  or total failures instead of a blanket success toast. */
export async function sendReportNow(
  clientIds?: number[],
): Promise<SendReportResult> {
  // The request helper auto-JSONifies `body` and sets Content-Type. When
  // clientIds is empty/missing we POST with no body so the backend takes the
  // legacy "all active clients" code path.
  if (clientIds && clientIds.length > 0) {
    return request<SendReportResult>("/v1/account/send-report", {
      method: "POST",
      body: { client_ids: clientIds },
    });
  }
  return request<SendReportResult>("/v1/account/send-report", {
    method: "POST",
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
}

export async function createClient(
  input: ClientCreateInput,
): Promise<ClientRow> {
  const res = await request<{ client: ClientRow }>("/v1/account/clients", {
    body: input,
  });
  return res.client;
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

/** Re-read a client's GMP auto-populate freshness (does not poll GMP). */
export async function refreshCapture(id: number): Promise<ClientRow> {
  const res = await request<{ client: ClientRow }>(
    `/v1/account/clients/${id}/refresh-capture`,
    { method: "POST" },
  );
  return res.client;
}

/** Send the client's latest report to the operator's own email (not the client).
 *  `toEmail` must match the operator's account email — validated server-side. */
export async function sendClientReportToMe(
  clientId: number,
  toEmail: string,
): Promise<{ ok: boolean; recipient: string }> {
  return request<{ ok: boolean; recipient: string }>(
    `/v1/account/clients/${clientId}/send-report?to=${encodeURIComponent(toEmail)}`,
    { method: "POST" },
  );
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
    clearSession();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
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
  return request<BulkDeleteResult>("/v1/account/clients/bulk", {
    method: "DELETE",
    body: { ids },
  });
}

export async function undoDelete(undoToken: string): Promise<{ ok: boolean; restored_arrays: number; restored_clients: number }> {
  return request("/v1/account/undo-delete", { body: { undo_token: undoToken } });
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

// ─── spreadsheet ingest (V4) ─────────────────────────────────────────────

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
}

export interface IngestPreview {
  source: "llm" | "heuristic" | "gmcs_shape";
  count: number;
  arrays: IngestRow[];
}

export interface IngestCommitResult {
  clients_created: number;
  arrays_created: number;
  accounts_created: number;
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
    clearSession();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
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
    clearSession();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    throw new UnauthorizedError();
  }
  if (!res.ok) throw new Error(await parseError(res));
  return (await res.json()) as NepoolPreviewResult;
}

export async function nepoolCommit(
  assignments: Array<{ array_id: number; nepool_gis_id: string }>,
): Promise<NepoolCommitResult> {
  return request<NepoolCommitResult>("/v1/account/nepool/commit", {
    body: { assignments },
  });
}
