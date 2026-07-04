// vaultBridge.ts — save a utility-portal login into the extension's
// CLIENT-SIDE encrypted vault from the dashboard, without the password ever
// touching our backend.
//
// The password flows page → so_bridge.js (content script) → background service
// worker, which encrypts it and STASHES it as a pending intent. Nothing is
// committed until the operator clicks Save in the EnergyAgent popup (a user
// gesture inside extension UI a web page cannot fake — the v1.9.109 hardening).
// So a dashboard field is exactly as safe as typing into the popup directly:
// the secret never reaches our servers, and a compromised page still can't
// silently overwrite a saved login.
//
// Resolves:
//   "pending"     → stashed; operator must confirm in the extension popup
//   "saved"       → committed immediately (older extension, no confirm step)
//   "unavailable" → no extension / no ack / refused — nothing was saved

export type VaultSaveResult = "pending" | "saved" | "unavailable";

const ACK_TIMEOUT_MS = 1500;

function reqId(): string {
  try {
    const c = typeof crypto !== "undefined" ? crypto : null;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    if (c && typeof (c as any).randomUUID === "function") return (c as any).randomUUID();
  } catch { /* fall through */ }
  return `v-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Save (stash) a portal login into the extension vault under `code`
 * ("gmp", "vec", "wec", …). The username identifies the credential slot, so a
 * new username for a utility becomes an additional per-client login rather than
 * overwriting an existing one (extension v1.9.111+ multi-slot vault).
 */
export function vaultStashLogin(
  code: string,
  username: string,
  password: string,
): Promise<VaultSaveResult> {
  return new Promise((resolve) => {
    const id = reqId();
    let done = false;

    function onAck(e: MessageEvent) {
      if (e.source !== window) return;
      const d = e.data;
      if (!d || d.type !== "SO_VAULT_ACK" || d.reqId !== id) return;
      window.removeEventListener("message", onAck);
      if (done) return;
      done = true;
      if (d.pending) resolve("pending");
      else if (d.ok) resolve("saved");
      else resolve("unavailable");
    }

    window.addEventListener("message", onAck);
    try {
      window.postMessage(
        { type: "SO_VAULT", op: "set", vendor: code, username, password, reqId: id },
        window.location.origin,
      );
    } catch {
      window.removeEventListener("message", onAck);
      resolve("unavailable");
      return;
    }

    window.setTimeout(() => {
      if (done) return;
      done = true;
      window.removeEventListener("message", onAck);
      resolve("unavailable");
    }, ACK_TIMEOUT_MS);
  });
}
