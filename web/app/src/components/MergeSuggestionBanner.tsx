// MergeSuggestionBanner — surfaces possible duplicate clients with a
// one-click merge or an explicit "Keep separate" dismissal that
// persists server-side so we never re-nag for the same pair.
//
// Placement: top of each ClientCard's expanded drawer. Only the top
// candidate is shown to keep noise low; if the operator dismisses it
// we re-fetch and surface the next-strongest match (if any).
//
// Banner is hidden when:
//   - server returned no candidates ≥ threshold
//   - operator dismissed every candidate this session
//   - we haven't finished loading suggestions yet

import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  type ClientRow,
  type MergeSuggestion,
  getMergeSuggestions,
  mergeClientInto,
  dismissMergeSuggestion,
} from "../lib/api";

interface Props {
  client: ClientRow;
  /** Called with the merged-into ClientRow when a merge succeeds. The
   *  parent should reload its clients list and expand the kept client. */
  onMerged: (dstClient: ClientRow, mergedFromId: number) => void;
}

export function MergeSuggestionBanner({ client, onMerged }: Props) {
  const toast = useToast();
  const [suggestions, setSuggestions] = useState<MergeSuggestion[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  // Session-local hide: when we just dismissed/merged we want the
  // banner to disappear immediately without waiting for the parent
  // reload that comes later.
  const [hiddenIds, setHiddenIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    getMergeSuggestions(client.id)
      .then((rows) => {
        if (!cancelled) setSuggestions(rows);
      })
      .catch(() => {
        // Suggestions are a nice-to-have; don't surface load errors.
        if (!cancelled) setSuggestions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [client.id]);

  if (!suggestions) return null;
  const visible = suggestions.filter((s) => !hiddenIds.has(s.id));
  if (visible.length === 0) return null;

  // Show only the top candidate to keep the drawer calm — the next one
  // (if any) replaces it after a dismissal.
  const top = visible[0];

  async function doMerge() {
    setBusy(top.id);
    try {
      // Merge the OTHER client INTO this one — current card is the
      // anchor the operator's already looking at. Their arrays come
      // here, the other row goes away.
      const dst = await mergeClientInto(top.id, client.id);
      toast.success(`Merged “${top.name}” into “${dst.name}”.`);
      setHiddenIds((prev) => new Set(prev).add(top.id));
      onMerged(dst, top.id);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't merge those clients.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function doDismiss() {
    setBusy(top.id);
    try {
      await dismissMergeSuggestion(client.id, top.id);
      setHiddenIds((prev) => new Set(prev).add(top.id));
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't dismiss this suggestion.",
      );
    } finally {
      setBusy(null);
    }
  }

  const portals = [
    top.has_gmp ? "GMP" : null,
    top.has_vec ? "VEC" : null,
  ].filter(Boolean).join(" + ");

  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-amber-900">
            Possible duplicate: <span className="font-semibold">{top.name}</span>
            {portals && (
              <span className="ml-1.5 text-xs font-medium text-amber-700">
                ({portals})
              </span>
            )}
          </p>
          <p className="mt-0.5 text-xs text-amber-800">
            {top.reasons.length > 0
              ? `Match signals: ${top.reasons.join(", ")}.`
              : "These two look like the same client."}{" "}
            Merging moves all arrays under “{client.name}” and removes the duplicate.
          </p>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button
          onClick={doMerge}
          disabled={busy !== null}
          className="h-8 px-3 text-xs"
        >
          {busy === top.id ? (
            <>
              <Spinner className="h-3.5 w-3.5" />
              Merging…
            </>
          ) : (
            `Merge into “${client.name}”`
          )}
        </Button>
        <button
          type="button"
          onClick={doDismiss}
          disabled={busy !== null}
          className="rounded text-xs font-medium text-amber-700 underline-offset-2 hover:text-amber-900 hover:underline focus:outline-none disabled:opacity-50"
        >
          Keep separate
        </button>
        {visible.length > 1 && (
          <span className="ml-auto text-[11px] text-amber-700">
            {visible.length - 1} more suggestion
            {visible.length - 1 === 1 ? "" : "s"}
          </span>
        )}
      </div>
    </div>
  );
}
