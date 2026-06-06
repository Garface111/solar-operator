// CaptureListener — globally watches for SO_CAPTURE_LANDED postMessages
// from the extension and turns them into toasts on the dashboard.
//
// Placement: mounted once near the top of ClientsSection. The Add Client
// modal closes immediately after the operator picks a portal, so the
// captured-state UI no longer lives in the modal. Instead, when the
// extension finishes scraping, this listener:
//
//   - Reloads the parent's clients list via onCaptureLanded()
//   - Diffs against the snapshot the modal saved to sessionStorage at
//     pick-time (under "so_capture_pending")
//   - Toasts the right thing:
//       * NEW client → green "✓ <name> added — they're on your dashboard"
//       * Same client re-scraped → amber "Looks like you re-captured <X>;
//         sign out of the portal first to add a different client"
//       * Failed sync (ok:false) → red error
//
// This decouples "where success is shown" from "where the user is when
// success happens" — the operator can be in any tab; the next time
// they look at the dashboard, they see the toasts queued up.

import { useEffect, useRef } from "react";
import { useToast } from "../ui/Toast";

interface Props {
  /** Called when a capture lands — parent should reload clients and
   *  return the fresh rows so we can detect "no new client" cases. */
  onCaptureLanded: () => Promise<{ id: number; name: string }[]>;
}

interface PendingCapture {
  provider: string;
  startedAt: number;
  knownIds: number[];
}

const PENDING_KEY = "so_capture_pending";
const PENDING_TTL_MS = 10 * 60 * 1000; // 10 minutes

function readPending(): PendingCapture | null {
  try {
    const raw = sessionStorage.getItem(PENDING_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingCapture;
    if (
      !parsed ||
      typeof parsed.startedAt !== "number" ||
      Date.now() - parsed.startedAt > PENDING_TTL_MS
    ) {
      sessionStorage.removeItem(PENDING_KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function clearPending() {
  try { sessionStorage.removeItem(PENDING_KEY); } catch { /* ignore */ }
}

export function CaptureListener({ onCaptureLanded }: Props) {
  const toast = useToast();
  // Guard against re-running on every render — handlers are stable.
  const handlerRef = useRef<((e: MessageEvent) => void) | null>(null);

  useEffect(() => {
    async function handler(e: MessageEvent) {
      if (e.source !== window) return;
      const data = e.data;
      if (!data || data.type !== "SO_CAPTURE_LANDED") return;

      // Failed sync — surface the error and don't reload.
      if (data.ok === false) {
        toast.error(
          typeof data.error === "string"
            ? `Capture failed: ${data.error}`
            : "Capture failed — try signing in again, or add the client manually.",
        );
        clearPending();
        try { window.dispatchEvent(new CustomEvent("so:capture-cleared")); } catch { /* ignore */ }
        return;
      }

      // Reload clients and figure out what actually landed.
      let freshRows: { id: number; name: string }[] = [];
      try {
        freshRows = await onCaptureLanded();
      } catch {
        /* parent surfaces its own errors */
      }

      // Check if server explicitly said this was an update/noop (not a new client)
      const serverSaysUpdate = data.is_new_client === false;

      const pending = readPending();
      if (!pending) {
        // Capture landed but we don't know which client the operator
        // was adding (ambient resync, page reload between pick and
        // landing, etc.). Quiet info toast — not an error.
        if (serverSaysUpdate) {
          const name = data.client_name ? data.client_name : "client data";
          toast.show(`Updated ${name} — latest data captured.`, "info");
        } else {
          toast.show("Captured fresh utility data.", "info");
        }
        try { window.dispatchEvent(new CustomEvent("so:capture-cleared")); } catch { /* ignore */ }
        return;
      }

      const before = new Set(pending.knownIds);
      const newRows = freshRows.filter((c) => !before.has(c.id));
      clearPending();

      if (newRows.length === 0) {
        if (serverSaysUpdate) {
          // Server confirmed this was an intentional re-capture/update — not a mistake
          const name = data.client_name ? data.client_name : "existing client";
          toast.show(`Updated ${name} — latest data captured.`, "info");
        } else {
          // We don't know if this was intentional — show the existing warning
          toast.warning(
            "Looks like the extension re-captured a client you already have. " +
            "Sign out of the portal in that tab first, then click Add client again.",
          );
        }
        try { window.dispatchEvent(new CustomEvent("so:capture-cleared")); } catch { /* ignore */ }
        return;
      }

      const newRow = newRows[0];
      const extra = newRows.length > 1 ? ` (+${newRows.length - 1} more)` : "";
      toast.success(`${newRow.name} added${extra} — they're on your dashboard.`);
      // Persist a short-lived flag so listeners that mounted AFTER the
      // SO_CAPTURE_LANDED message can still discover the recent capture and
      // refresh their state (e.g. SandboxCanvas mounted post-event during a
      // first-visit redirect). The flag carries a timestamp so it can self-expire.
      try { localStorage.setItem("so:capture:landed:ts", String(Date.now())); } catch { /* ignore */ }
      try { window.dispatchEvent(new CustomEvent("so:capture-cleared")); } catch { /* ignore */ }
    }
    handlerRef.current = handler;
    window.addEventListener("message", handler);
    return () => {
      window.removeEventListener("message", handler);
      handlerRef.current = null;
    };
  }, [onCaptureLanded, toast]);

  return null;
}
