// Shared onboarding-wizard helpers: token persistence + relative-path API calls.
// All endpoints are same-origin (FastAPI serves this SPA), so plain fetch works.

const TOKEN_KEY = "onboarding_token";

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

export interface CheckoutResponse {
  checkout_url: string;
  onboarding_token: string;
}

export async function createCheckout(body: {
  email: string;
  full_name: string;
  company?: string;
}): Promise<CheckoutResponse> {
  const res = await fetchWithTimeout("/v1/onboarding/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
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
}

export interface ClientPayload {
  name: string;
  contact_email?: string;
  gmp_email?: string;
  gmp_username?: string;
  gmp_autopopulate: boolean;
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

export async function completeOnboarding(token: string): Promise<CompleteResponse> {
  const res = await fetchWithTimeout(`/v1/onboarding/complete?token=${encodeURIComponent(token)}`, {
    method: "POST",
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
