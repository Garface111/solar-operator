// openPortalTab.ts — see /web/app/src/lib/openPortalTab.ts for the canonical doc.
// Duplicated here because the onboarding app is built independently.

const ACK_TIMEOUT_MS = 250;

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
