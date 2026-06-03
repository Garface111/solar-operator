// Shared onboarding-wizard helpers: token persistence + relative-path API calls.
// All endpoints are same-origin (FastAPI serves this SPA), so plain fetch works.

const TOKEN_KEY = "onboarding_token";

/** Persist the onboarding token for the rest of the wizard (survives the Stripe round-trip). */
export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token);
}

/**
 * Resolve the onboarding token, preferring a `?onboarding_token=` query param
 * (Stripe's success_url carries it back) and falling back to sessionStorage.
 * A token found in the URL is persisted so later screens can read it.
 */
export function getToken(): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get("onboarding_token");
  if (fromUrl) {
    setToken(fromUrl);
    return fromUrl;
  }
  return sessionStorage.getItem(TOKEN_KEY);
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
  const res = await fetch("/v1/onboarding/checkout", {
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
  const res = await fetch(`/v1/onboarding/extension-ping?token=${encodeURIComponent(token)}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}

export async function markExtensionInstalled(token: string): Promise<void> {
  const res = await fetch(
    `/v1/onboarding/extension-installed?token=${encodeURIComponent(token)}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error(await parseError(res));
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
  gmp_autopopulate: boolean;
  arrays: ArrayPayload[];
}

export async function submitClients(token: string, clients: ClientPayload[]): Promise<void> {
  const res = await fetch(`/v1/onboarding/clients?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(clients),
  });
  if (!res.ok) throw new Error(await parseError(res));
}

export async function completeOnboarding(token: string): Promise<void> {
  const res = await fetch(`/v1/onboarding/complete?token=${encodeURIComponent(token)}`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await parseError(res));
}

export interface OnboardingStatus {
  stage: string;
  tenant_id: string;
  active: boolean;
  activation_code: string | null;
  clients_count: number;
  arrays_count: number;
}

export async function fetchStatus(token: string): Promise<OnboardingStatus> {
  const res = await fetch(`/v1/onboarding/status?token=${encodeURIComponent(token)}`);
  if (!res.ok) throw new Error(await parseError(res));
  return res.json();
}
