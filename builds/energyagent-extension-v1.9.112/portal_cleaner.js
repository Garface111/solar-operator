// portal_cleaner.js — runs at document_start on GMP and VEC pages.
//
// When AddClientByLoginModal fires SO_WIPE_COOKIES, background.js wipes
// session cookies AND sets a so_pending_storage_wipe flag in chrome.storage.
// This script checks that flag on the first document_start of the portal
// page — which is BEFORE any portal JS runs — and clears localStorage +
// sessionStorage so JWTs or auth state cached there don't re-authenticate
// the previous customer's session.
//
// The flag is consumed (deleted) immediately so subsequent page loads on
// the same portal (e.g. navigating within GMP after sign-in) are not wiped.
// Flag has a 30s TTL as a belt-and-suspenders guard against orphaned flags.

(async () => {
  try {
    const s = await chrome.storage.local.get("so_pending_storage_wipe");
    const flag = s.so_pending_storage_wipe;
    if (!flag || typeof flag.domain !== "string" || typeof flag.ts !== "number") return;

    const host = location.hostname;
    const matchesGmp = flag.domain === "greenmountainpower.com" && host.endsWith("greenmountainpower.com");
    const matchesVec = flag.domain === "smarthub.coop" && host.endsWith("smarthub.coop");
    if (!matchesGmp && !matchesVec) return;

    if (Date.now() - flag.ts > 30000) {
      // Stale flag — clean up and skip.
      await chrome.storage.local.remove("so_pending_storage_wipe");
      return;
    }

    // Consume the flag FIRST so a crash mid-clear doesn't re-trigger.
    await chrome.storage.local.remove("so_pending_storage_wipe");

    try { localStorage.clear(); } catch (_) { /* cross-origin guard, ignore */ }
    try { sessionStorage.clear(); } catch (_) { /* cross-origin guard, ignore */ }

    console.log(`[SO ${Date.now()}] portal_cleaner: storage cleared on ${host}`);
  } catch (e) {
    // Non-fatal — the cookie wipe is the primary defence.
    console.warn("[SO] portal_cleaner error:", e);
  }
})();
