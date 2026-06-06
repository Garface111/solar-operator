// openPortalTab.ts — see /web/app/src/lib/openPortalTab.ts for the canonical doc.
// Duplicated here because the onboarding app is built independently.

const ACK_TIMEOUT_MS = 250;

// See web/app/src/lib/openPortalTab.ts for the full rationale.
// Short version: v < 1.4.6 leaves GMP localStorage stale → 404 JSON page at /account/.
// Fall back to root (/) which redirects to login instead of hitting the broken API path.

export const GMP_ACCOUNT_URL = "https://greenmountainpower.com/account/";
export const GMP_SAFE_URL = "https://greenmountainpower.com/";

function supportsLocalStorageWipe(version: string): boolean {
  const parts = version.split(".").map(Number);
  const ma = parts[0] ?? 0;
  const mi = parts[1] ?? 0;
  const pa = parts[2] ?? 0;
  if (ma !== 1) return ma > 1;
  if (mi !== 4) return mi > 4;
  return pa >= 6;
}

export function gmpPortalUrl(version: string | null): string {
  if (version && supportsLocalStorageWipe(version)) return GMP_ACCOUNT_URL;
  return GMP_SAFE_URL;
}

function uuid(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const c: any = (typeof crypto !== "undefined" ? crypto : null);
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch { /* fall through */ }
  return `r-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function openPortalTab(url: string): Promise<"extension" | "fallback" | "blocked"> {
  return new Promise((resolve) => {
    const reqId = uuid();
    let settled = false;

    const onMessage = (event: MessageEvent) => {
      if (event.source !== window) return;
      const data = event.data;
      if (!data || typeof data !== "object") return;
      if (data.type !== "SO_OPEN_PORTAL_ACK") return;
      if (data.reqId !== reqId) return;
      window.removeEventListener("message", onMessage);
      if (settled) return;
      settled = true;
      if (data.ok) {
        resolve("extension");
      } else {
        const w = window.open(url, "_blank", "noopener,noreferrer");
        resolve(w ? "fallback" : "blocked");
      }
    };

    window.addEventListener("message", onMessage);
    window.postMessage({ type: "SO_OPEN_PORTAL", url, reqId }, "*");

    window.setTimeout(() => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", onMessage);
      const w = window.open(url, "_blank", "noopener,noreferrer");
      resolve(w ? "fallback" : "blocked");
    }, ACK_TIMEOUT_MS);
  });
}
