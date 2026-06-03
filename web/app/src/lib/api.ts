// Dashboard API client + session handling.
//
// The SPA is served same-origin by FastAPI (mounted at /app/), so every call is
// a relative-path fetch. The session token lives in localStorage under
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

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (!opts.noAuth) {
    const token = getSession();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const res = await fetch(path, {
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

export async function requestLoginLink(email: string): Promise<void> {
  await request("/v1/auth/request", { body: { email }, noAuth: true });
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
  last_pull_at: string | null;
  last_delivery_at: string | null;
  created_at: string | null;
  accounts_count: number;
  bills_count: number;
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

export async function deleteClient(id: number): Promise<void> {
  await request(`/v1/account/clients/${id}`, { method: "DELETE" });
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
): Promise<void> {
  await request(`/v1/account/clients/${clientId}/arrays/${arrayId}`, {
    method: "DELETE",
  });
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
}

export async function listProviders(): Promise<Provider[]> {
  const res = await request<{ providers: Provider[] }>("/v1/providers", {
    noAuth: true,
  });
  return res.providers;
}
