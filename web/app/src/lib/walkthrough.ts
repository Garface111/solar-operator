export const WALKTHROUGH_KEY = "so_walkthrough_v1_seen";

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
