// v2 (Jun 2026): post-sublime-onboarding rewrite. Old key 'so_walkthrough_v1_seen'
// was tied to the placeholder-rename arc which no longer fires for fresh users.
// Bump to v2 so existing operators see the new dashboard tour too.
export const WALKTHROUGH_KEY = "so_walkthrough_v2_seen";

export function hasSeenWalkthrough(): boolean {
  try {
    return window.localStorage.getItem(WALKTHROUGH_KEY) === "true";
  } catch {
    return false;
  }
}

export function markWalkthroughSeen(): void {
  try {
    window.localStorage.setItem(WALKTHROUGH_KEY, "true");
  } catch {
    /* ignore quota errors */
  }
}
