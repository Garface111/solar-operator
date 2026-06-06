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
    clearSession();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    throw new UnauthorizedError();
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

export async function requestLoginLink(email: string, persist = true): Promise<void> {
  await request("/v1/auth/request", { body: { email, persist }, noAuth: true });
}

/** Sign in with email + password. Returns session token on success. */
export async function passwordLogin(
  email: string,
  password: string,
): Promise<string> {
  const res = await request<{ session_token: string }>(
    "/v1/auth/password-login",
    { body: { email, password }, noAuth: true },
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
  email: string | null;
  plan: string | null;
  active: boolean;
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
  created_at: string | null;
  trial_ends_at: string | null;
  accounts_count: number;
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

export async function updateAccountName(name: string): Promise<string> {
  const res = await request<{ name: string }>("/v1/account/name", {
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
 *  sendMode, when provided, is saved as the tenant default before delivery.
 *  Returns the per-client outcome so the UI can tell the truth about partial
 *  or total failures instead of a blanket success toast. */
export async function sendReportNow(
  clientIds?: number[],
  sendMode?: string,
): Promise<SendReportResult> {
  const payload: Record<string, unknown> = {};
  if (clientIds && clientIds.length > 0) payload.client_ids = clientIds;
  if (sendMode) payload.send_mode = sendMode;
  const hasPayload = Object.keys(payload).length > 0;
  return request<SendReportResult>("/v1/account/send-report", {
    method: "POST",
    ...(hasPayload ? { body: payload } : {}),
  });
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

// ─── production ──────────────────────────────────────────────────────────

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
}

export interface DailyCoverage {
  day_count: number;
  first_day: string | null;
  last_day: string | null;
  source_counts: Record<string, number>;
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
    clearSession();
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
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
