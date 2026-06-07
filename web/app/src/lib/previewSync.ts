/**
 * previewSync — iframe navigation sync for the MC Lens-Picker live preview.
 *
 * When MC loads this SPA inside an iframe for preview comparison, both the
 * production iframe and the proposed iframe run this module. It:
 *   1. Posts the current pathname + scrollY whenever either changes, so MC
 *      can forward the navigation to the other iframe.
 *   2. Listens for incoming mc:sync messages from MC and applies them.
 *
 * The sourceId ('production' | 'proposed') is injected via URL query param:
 *   /accounts/?mc_preview_id=production
 *   /accounts/preview/42/?mc_preview_id=proposed
 *
 * MC's useSyncedIframes hook reads the mc:nav events and posts mc:sync back.
 * If the query param is absent this module is a no-op — production traffic
 * is unaffected.
 */

const PREVIEW_ID_PARAM = "mc_preview_id";

function getPreviewId(): string | null {
  try {
    const params = new URLSearchParams(window.location.search);
    return params.get(PREVIEW_ID_PARAM);
  } catch {
    return null;
  }
}

let _initialized = false;

export function initPreviewSync(): void {
  const sourceId = getPreviewId();
  if (!sourceId || _initialized) return;
  _initialized = true;

  // Post current location whenever the URL changes (hash, pushState, replaceState)
  function postNav(): void {
    window.parent.postMessage(
      {
        type: "mc:nav",
        sourceId,
        pathname: window.location.pathname,
        scrollY: window.scrollY,
      },
      "*",
    );
  }

  // Override pushState / replaceState to capture SPA navigations
  const _push = history.pushState.bind(history);
  const _replace = history.replaceState.bind(history);
  history.pushState = (...args) => { _push(...args); postNav(); };
  history.replaceState = (...args) => { _replace(...args); postNav(); };

  window.addEventListener("popstate", postNav);
  window.addEventListener("scroll", () => postNav(), { passive: true });

  // Listen for incoming sync commands from MC
  window.addEventListener("message", (e: MessageEvent) => {
    if (!e.data || e.data.type !== "mc:sync") return;
    const { pathname, scrollY } = e.data as { pathname: string; scrollY: number };
    if (pathname && pathname !== window.location.pathname) {
      history.replaceState(null, "", pathname);
    }
    if (typeof scrollY === "number") {
      window.scrollTo({ top: scrollY, behavior: "instant" });
    }
  });

  // Post initial state
  postNav();
}
