// openPortalTab.ts — open the utility portal in a way that prefers the
// Chrome extension's background-tab path, falling back to a normal new
// tab if the extension isn't installed/listening.
//
// Why this exists: when the operator clicks "Open Green Mountain Power"
// from the dashboard, we want the utility tab to load IN THE BACKGROUND
// so they keep watching the SPA's "waiting for capture" indicator and
// see the moment data lands. window.open() always foregrounds the new
// tab — only chrome.tabs.create({active:false}) can background it, and
// that lives in the extension. We postMessage to the so_bridge content
// script and wait briefly for an ack; if no ack arrives, the extension
// isn't there and we fall back to a foreground window.open.

const ACK_TIMEOUT_MS = 250;

// ── GMP portal URL helper ─────────────────────────────────────────────
//
// v1.4.5 wipes cookies before opening the GMP tab but does NOT wipe
// localStorage. GMP's Vue SPA caches an auth token in localStorage; when
// the cookie is gone but localStorage is stale, the SPA boots at /account/,
// hits its own backend with the dead token, gets a JSON {"detail":"Not Found"}
// 404, and the browser renders that raw — a black JSON-viewer page.
//
// v1.4.6 added portal_cleaner.js (runs at document_start) which wipes both
// localStorage and sessionStorage on the SO_WIPE_COOKIES event. Once the user
// is on v1.4.6+ it is safe to send them straight to /account/.
//
// For v1.4.5 and earlier (or when version is unknown) we fall back to the
// marketing root https://greenmountainpower.com/. That page does a
// cookie-based auth check on load; with the cookie wiped it redirects to the
// login screen rather than making a direct API call that returns 404.

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

/** Return the right GMP landing URL for the installed extension version.
 *  v >= 1.4.6 → /account/ (localStorage wiped by portal_cleaner.js).
 *  v <  1.4.6 or unknown → / (safe root, redirects to login on stale auth). */
export function gmpPortalUrl(version: string | null): string {
  if (version && supportsLocalStorageWipe(version)) return GMP_ACCOUNT_URL;
  return GMP_SAFE_URL;
}

// ── Pattern-A cookie wipe ─────────────────────────────────────────────
//
// Used by AddClientByLoginModal when it needs a FOREGROUND tab with a
// guaranteed-clean session. The caller opens about:blank synchronously
// in the click handler (popup-blocker-safe), then awaits this function
// before setting newTab.location.href.
//
// Sends SO_WIPE_COOKIES and resolves when SO_WIPE_COOKIES_ACK arrives or
// after WIPE_TIMEOUT_MS — whichever comes first. If the extension is
// absent the timeout fires and the caller navigates anyway (best-effort,
// same behaviour as the old fire-and-forget).

export const WIPE_TIMEOUT_MS = 800;

let _wipeCtr = 0;

export function wipeCookiesAndWait(domain: string): Promise<void> {
  return new Promise((resolve) => {
    const reqId = `w-${Date.now()}-${++_wipeCtr}`;
    let done = false;

    function onAck(e: MessageEvent) {
      if (e.source !== window) return;
      const d = e.data;
      if (!d || d.type !== "SO_WIPE_COOKIES_ACK" || d.reqId !== reqId) return;
      window.removeEventListener("message", onAck);
      if (done) return;
      done = true;
      resolve();
    }

    window.addEventListener("message", onAck);
    try {
      window.postMessage({ type: "SO_WIPE_COOKIES", domain, reqId }, "*");
    } catch {
      window.removeEventListener("message", onAck);
      resolve();
      return;
    }

    window.setTimeout(() => {
      if (done) return;
      done = true;
      window.removeEventListener("message", onAck);
      resolve();
    }, WIPE_TIMEOUT_MS);
  });
}

function uuid(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const c: any = (typeof crypto !== "undefined" ? crypto : null);
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch { /* fall through */ }
  return `r-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Try to open `url` in a tab via the Solar Operator extension. When
 * `active` is true the new tab is foregrounded (use for flows where
 * the operator is about to interact, like Add Client → sign-in).
 * Defaults to background-tab for ambient captures.
 *
 * Returns a promise that resolves to:
 *   - "extension"  → tab opened via the extension
 *   - "fallback"   → no extension; opened a normal foreground tab
 *   - "blocked"    → fallback was blocked by the popup blocker
 */
export function openPortalTab(
  url: string,
  opts: { active?: boolean } = {},
): Promise<"extension" | "fallback" | "blocked"> {
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
        // Extension is there but refused (bad URL, etc.) — fall back.
        const w = window.open(url, "_blank", "noopener,noreferrer");
        resolve(w ? "fallback" : "blocked");
      }
    };

    window.addEventListener("message", onMessage);
    window.postMessage(
      { type: "SO_OPEN_PORTAL", url, reqId, active: !!opts.active },
      "*",
    );

    window.setTimeout(() => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", onMessage);
      const w = window.open(url, "_blank", "noopener,noreferrer");
      resolve(w ? "fallback" : "blocked");
    }, ACK_TIMEOUT_MS);
  });
}
