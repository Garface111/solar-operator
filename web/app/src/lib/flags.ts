/**
 * Runtime feature flags — OFF by default, flippable per-browser so a dark-shipped
 * feature can be built and tested on prod without being live for real operators.
 *
 * To test a flagged feature (e.g. in the demo account), run in the browser console:
 *   localStorage.setItem('so:flag:cloud-capture-ui', 'on'); location.reload();
 *
 * When a flag graduates to GA, hard-code its return to `true` here (or drive it
 * from a backend/tenant allowlist) — one place to flip.
 */
function flagOn(key: string): boolean {
  try {
    return localStorage.getItem(key) === "on";
  } catch {
    return false;
  }
}

/**
 * Cloud Capture — the server-side portal-harvesting Credential Vault UI (NEPOOL).
 * Dark-shipped: the backend (`/v1/cloud-capture/*` + the harvester) is already
 * product-agnostic; this gate hides the operator-facing UI until it's tested.
 */
export function cloudCaptureUiEnabled(): boolean {
  return flagOn("so:flag:cloud-capture-ui");
}
