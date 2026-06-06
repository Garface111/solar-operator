/**
 * lazyWithRetry — wrapper around React.lazy() that survives stale-chunk
 * errors after a deploy.
 *
 * Problem (Bruce Jun6'26): when Railway swaps the SPA build, the new
 * index.html references new chunk hashes (e.g. ImportSpreadsheetModal-XYZ).
 * Any browser that loaded the OLD index.html keeps a reference to the OLD
 * chunk name. Clicking a button that lazy-imports the modal → 404 → React
 * throws ChunkLoadError → white screen until the user refreshes manually.
 *
 * Fix: when a chunk import fails AND we haven't already retried this session,
 * stash a sentinel in sessionStorage and reload the page. Reload pulls the
 * new index.html which references the new chunk names → next import succeeds.
 *
 * If the retry-reload itself fails (network down, server actually broken),
 * we bubble the error so an ErrorBoundary can show a useful message
 * instead of looping forever.
 */
import { lazy, type ComponentType } from "react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type LazyComponent<T extends ComponentType<any>> = ReturnType<typeof lazy<T>>;

const RETRY_KEY = "so:chunk-retry";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function lazyWithRetry<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
): LazyComponent<T> {
  return lazy(async () => {
    try {
      const mod = await factory();
      // Successful load → clear any retry sentinel so future failures can retry.
      try {
        window.sessionStorage.removeItem(RETRY_KEY);
      } catch {
        /* private mode / disabled storage — ignore */
      }
      return mod;
    } catch (err) {
      let alreadyRetried = false;
      try {
        alreadyRetried = window.sessionStorage.getItem(RETRY_KEY) === "1";
      } catch {
        /* ignore */
      }
      if (!alreadyRetried) {
        try {
          window.sessionStorage.setItem(RETRY_KEY, "1");
        } catch {
          /* ignore */
        }
        // Hard reload picks up the fresh index.html + new chunk hashes.
        window.location.reload();
        // Return a never-resolving promise so React doesn't render an
        // error fallback before the reload completes.
        return new Promise<{ default: T }>(() => {});
      }
      // Already retried this session → real failure. Let ErrorBoundary handle.
      throw err;
    }
  });
}
