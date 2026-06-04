export interface PollerHandle {
  cancel: () => void;
}

/** Poll `fetcher` every `intervalMs` (default 2s) for up to `timeoutMs` (default 30s).
 *  Resolves with the first response where `isChanged(prev, next)` is true, or null on
 *  timeout/cancel. `prev` advances to `next` after each non-change poll.
 *
 *  Returns a tuple of [promise, handle]. Call handle.cancel() to stop early (e.g. on
 *  component unmount). */
export function pollUntilChanged<T>(
  fetcher: () => Promise<T>,
  isChanged: (prev: T, next: T) => boolean,
  opts: { intervalMs?: number; timeoutMs?: number } = {},
): [Promise<T | null>, PollerHandle] {
  const intervalMs = opts.intervalMs ?? 2_000;
  const timeoutMs = opts.timeoutMs ?? 30_000;

  let cancelled = false;
  let timerId: ReturnType<typeof setTimeout> | null = null;
  let settled = false;
  let outerResolve!: (val: T | null) => void;

  const promise = new Promise<T | null>((resolve) => {
    outerResolve = resolve;
  });

  function settle(val: T | null) {
    if (settled) return;
    settled = true;
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
    outerResolve(val);
  }

  async function run() {
    let prev: T;
    try {
      prev = await fetcher();
    } catch {
      settle(null);
      return;
    }
    if (cancelled) {
      settle(null);
      return;
    }

    const deadline = Date.now() + timeoutMs;

    async function tick() {
      if (cancelled || Date.now() >= deadline) {
        settle(null);
        return;
      }
      let next: T;
      try {
        next = await fetcher();
      } catch {
        if (!cancelled) timerId = setTimeout(tick, intervalMs);
        return;
      }
      if (cancelled) {
        settle(null);
        return;
      }
      if (isChanged(prev, next)) {
        settle(next);
      } else {
        prev = next;
        timerId = setTimeout(tick, intervalMs);
      }
    }

    timerId = setTimeout(tick, intervalMs);
  }

  run();

  const handle: PollerHandle = {
    cancel() {
      cancelled = true;
      settle(null);
    },
  };

  return [promise, handle];
}
