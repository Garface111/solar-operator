// Shared onboarding-wizard helpers: token persistence + relative-path API calls.
// All endpoints are same-origin (FastAPI serves this SPA), so plain fetch works.

const TOKEN_KEY = "onboarding_token";

/**
 * Terms/Privacy + account-access authorization version the Welcome screen's
 * consent checkbox covers. Bump when tos.md / privacy.md change materially.
 * Mirrors the Array Operator client's CONSENT_VERSION so both products record
 * comparable proof-of-consent on the tenant. The backend REJECTS a signup
 * that arrives without this (fail-closed server-side consent gate).
 */
export const CONSENT_VERSION = "2026-06-27";

const CONSENT_KEY = "so_consent_version";

/**
 * Record (or clear) the user's affirmative Terms/Privacy acceptance. Written
 * to BOTH storages for the same fresh-tab-survival reasons as setToken.
 * HONESTY RULE: only ever stored when the box was genuinely ticked — the
 * stored value becomes the tenant's durable consent_version proof, so it must
 * never be fabricated for an unticked box (untick -> cleared).
 */
export function setConsentAccepted(accepted: boolean): void {
  try {
    if (accepted) localStorage.setItem(CONSENT_KEY, CONSENT_VERSION);
    else localStorage.removeItem(CONSENT_KEY);
  } catch {
    /* localStorage may be unavailable (private mode) — sessionStorage still set */
  }
  try {
    if (accepted) sessionStorage.setItem(CONSENT_KEY, CONSENT_VERSION);
    else sessionStorage.removeItem(CONSENT_KEY);
  } catch {
    /* ignore */
  }
}

/** The consent version the user accepted on the Welcome screen, or null. */
export function getAcceptedConsentVersion(): string | null {
  try {
    const v = localStorage.getItem(CONSENT_KEY);
    if (v) return v;
  } catch {
    /* fall through to sessionStorage */
  }
  try {
    return sessionStorage.getItem(CONSENT_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist the onboarding token for the rest of the wizard.
 *
 * Written to BOTH localStorage and sessionStorage: sessionStorage keeps the
 * existing single-tab flow working, while localStorage survives the Stripe
 * Checkout round-trip landing in a *fresh tab* (or a browser that opened the
 * payment page in a new context) — the #1 source of stranded onboarding.
 */
export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* localStorage may be unavailable (private mode) — sessionStorage still set */
  }
  sessionStorage.setItem(TOKEN_KEY, token);
}

/**
 * Resolve the onboarding token, preferring a `?onboarding_token=` query param
 * (Stripe's success_url carries it back), then localStorage (survives a
 * fresh-tab / interrupted-checkout return), then sessionStorage.
 * A token found in the URL is persisted so later screens can read it.
 */
export function getToken(): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get("onboarding_token");
  if (fromUrl) {
    setToken(fromUrl);
    return fromUrl;
  }
  return localStorage.getItem(TOKEN_KEY) ?? sessionStorage.getItem(TOKEN_KEY);
}

/** Default per-request timeout. A stalled connection should surface an error
 *  rather than hang the wizard on a spinner forever. */
const DEFAULT_TIMEOUT_MS = 30_000;

/** fetch() with an AbortController timeout, surfacing a clear user-facing error
 *  on abort instead of a bare DOMException. */
async function fetchWithTimeout(
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
      throw new Error("Request timed out — check your connection and try again.");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function parseError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) return body.detail.map((d: any) => d.msg).join("; ");
  } catch {
    /* fall through */
  }
  return `Request failed (${res.status})`;
}

export interface ArraySeedPayload {
  name: string;
  nepool_gis_id?: string;
  bill_offset_months?: number;
}

export interface ClientSeedPayload {
  name: string;
  contact_email?: string;
  arrays: ArraySeedPayload[];
}

export interface StartResponse {
  onboarding_token: string;
  tenant_id: string;
}

/**
 * No-upfront-payment signup. Creates a live, trialing tenant — no card is
 * collected. The 14-day trial starts immediately; the operator adds a payment
 * method later from the dashboard. Replaces the old createCheckout() →
 * Stripe-Checkout redirect.
 */
export async function startOnboarding(body: {
  email: string;
  full_name: string;
  company: string;
  /** Optional: password chosen on /info, hashed server-side. */
  password?: string;
  /** Operator's array estimate; quantity syncs to reality when real arrays are added. */
  array_count?: number;
  /** Terms/Privacy version accepted on Welcome; auto-attached from storage when omitted. */
  consent_version?: string;
}): Promise<StartResponse> {
  // Attach the consent recorded on the Welcome screen. Only sent when the box
  // was genuinely ticked (see setConsentAccepted) — the backend persists it as
  // proof of consent, and rejects the signup with a clear message without it.
  const consent = body.consent_version ?? getAcceptedConsentVersion() ?? undefined;
  const res = await fetchWithTimeout("/v1/onboarding/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, ...(consent ? { consent_version: consent } : {}) }),
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export interface CheckoutResponse {
  /** Always null now — no card is collected at signup. */
  checkout_url: string | null;
  onboarding_token: string;
  tenant_id?: string;
}

/**
 * @deprecated Card collection was removed from signup. Thin wrapper around
 * startOnboarding() kept only for any stale caller; returns checkout_url=null.
 * ClientSetup now calls startOnboarding() directly.
 */
export async function createCheckout(body: {
  email: string;
  full_name: string;
  company: string;
  array_count?: number;
}): Promise<CheckoutResponse> {
  const { onboarding_token, tenant_id } = await startOnboarding(body);
  return { checkout_url: null, onboarding_token, tenant_id };
}

export interface ExtensionPing {
  installed: boolean;
  last_capture_at: string | null;
}

export async function pingExtension(token: string): Promise<ExtensionPing> {
  const res = await fetchWithTimeout(`/v1/onboarding/extension-ping?token=${encodeURIComponent(token)}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function markExtensionInstalled(
  token: string,
  sessionId?: string | null,
): Promise<void> {
  // Pass the Stripe session_id when we have it so a webhook-lagged tenant can
  // self-heal instead of getting a 402 "complete payment" error mid-onboarding.
  const sid = sessionId
    ? `&session_id=${encodeURIComponent(sessionId)}`
    : "";
  const res = await fetchWithTimeout(
    `/v1/onboarding/extension-installed?token=${encodeURIComponent(token)}${sid}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await parseError(res));
}

/** Self-heal a paid-but-inactive tenant via the Stripe Checkout session_id.
 *  Idempotent; always resolves to the current onboarding status. */
export async function reconcileCheckout(
  token: string,
  sessionId: string,
): Promise<OnboardingStatus> {
  const res = await fetchWithTimeout(
    `/v1/onboarding/reconcile-checkout?token=${encodeURIComponent(token)}` +
      `&session_id=${encodeURIComponent(sessionId)}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export interface ConnectionTest {
  connected: boolean;
  captures_count: number;
  last_capture_at: string | null;
}

/** In-flow check: has the extension captured a GMP session for this tenant in
 *  the last 5 minutes? */
export async function testConnection(token: string): Promise<ConnectionTest> {
  const res = await fetchWithTimeout(
    `/v1/onboarding/test-connection?token=${encodeURIComponent(token)}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export interface ArrayPayload {
  name: string;
  nepool_gis_id?: string;
  bill_offset_months?: number;
  /** V2: generation source — solar|wind|hydro|digester|storage. Omit for solar. */
  fuel_type?: string;
}

export interface ClientPayload {
  name: string;
  contact_email?: string;
  gmp_email?: string;
  gmp_username?: string;
  gmp_autopopulate: boolean;
  /** V2: the kind of generation this client reports. Seeds manually-added
   *  arrays and lets the backend tag auto-populated arrays. Omit for solar. */
  default_fuel_type?: string;
  arrays: ArrayPayload[];
}

export async function submitClients(token: string, clients: ClientPayload[]): Promise<void> {
  const res = await fetchWithTimeout(`/v1/onboarding/clients?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(clients),
  });
  if (!res.ok) throw new Error(await parseError(res));
}

export interface CompleteResponse {
  ok: boolean;
  // Fresh dashboard session bound to this tenant. Stash it as `so_session` so
  // the operator lands on the dashboard already signed in — no email detour.
  session_token?: string;
  magic_link_email_sent?: boolean;
  sample_email_sent?: boolean;
}

export async function completeOnboarding(
  token: string,
  opts?: { password?: string },
): Promise<CompleteResponse> {
  const body = opts?.password ? { password: opts.password } : undefined;
  const res = await fetchWithTimeout(`/v1/onboarding/complete?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export interface OnboardingStatus {
  stage: string;
  tenant_id: string;
  active: boolean;
  activation_code: string | null;
  clients_count: number;
  arrays_count: number;
  extension_active: boolean;
  extension_heartbeat_at: string | null;
}

export async function fetchStatus(token: string): Promise<OnboardingStatus> {
  const res = await fetchWithTimeout(`/v1/onboarding/status?token=${encodeURIComponent(token)}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

// ─── Utility provider catalog (public; powers the home-page "is my utility
//     supported?" search) ──────────────────────────────────────────────────

/** scrape_status from the backend catalog. `live` = automated capture works
 *  today; `in-progress` = portal known, adapter not yet built (manual upload
 *  fallback); `manual` = no portal, PDFs only. Mirrors api/providers.py. */
export type ProviderStatus = "live" | "in-progress" | "manual";

export interface Provider {
  code: string;
  label: string;
  state: string;
  scrape_status: ProviderStatus;
  smarthub_host: string;
  portal_url: string;
  notes: string;
}

/** Fetch the full supported-utility catalog. Public, no auth — safe to call
 *  before signup. Cached by the caller; the list is ~1.4k rows but tiny. */
export async function fetchProviders(): Promise<Provider[]> {
  const res = await fetchWithTimeout("/v1/providers");
  if (!res.ok) throw new Error(await parseError(res));
  const body = await res.json();
  return (body?.providers ?? []) as Provider[];
}

export interface UtilityRequestInput {
  utility_name: string;
  region?: string;
  email?: string;
  notes?: string;
  willing_to_help?: boolean;
}

/** Submit a "please add my utility" request from the public home page. No auth.
 *  Emails Ford and (when configured) fires the add-a-utility agent webhook. */
export async function requestUtility(
  input: UtilityRequestInput,
): Promise<{ ok: boolean; agent_dispatched?: boolean }> {
  const res = await fetchWithTimeout("/v1/onboarding/request-utility", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

// ─── Cloud Capture — "store it with us" onboarding fork ─────────────────────
// The cloud step completes onboarding EARLY (to mint a dashboard session it can
// store credentials with), so /done must NOT complete a second time and fire a
// duplicate magic-link/sample email. It checks this flag.
export const ONBOARDING_COMPLETED_KEY = "so:onboarding:completed";

export interface CloudCredentialInput {
  provider: string;
  username: string;
  password: string;
  login_host?: string | null;
}

/** Store a utility login server-side (encrypted at rest). Authed with the
 *  dashboard session_token from completeOnboarding(). Consent is explicit. */
export async function saveCloudCredential(
  sessionToken: string,
  input: CloudCredentialInput,
): Promise<{ ok: boolean; error?: string }> {
  const res = await fetchWithTimeout("/v1/cloud-capture/credentials", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${sessionToken}`,
    },
    body: JSON.stringify({ ...input, enable: true, consent: true }),
  });
  let detail = `http_${res.status}`;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") detail = body.detail;
    else if (typeof body?.error === "string") detail = body.error;
  } catch {
    /* non-JSON */
  }
  return { ok: res.ok, error: res.ok ? undefined : detail };
}

/** Persist capture_mode=cloud so the dashboard opens the Cloud Capture vault. */
export async function setCaptureModeCloud(sessionToken: string): Promise<void> {
  await fetchWithTimeout("/v1/account/capture-mode", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${sessionToken}`,
    },
    body: JSON.stringify({ mode: "cloud" }),
  });
}
