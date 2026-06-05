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
