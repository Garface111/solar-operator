// ArrayMergeSuggestionBanner — surfaces possible duplicate arrays
// within the same tenant (commonly: an auto-created "<account number>"
// array vs an operator-imported "<friendly name>" array carrying the
// same NEPOOL-GIS ID). One-click merge or a persistent dismissal.
//
// Mounted at the TOP of an ArrayList so the operator sees it before
// scrolling through the per-array rows. Only the top candidate is
// shown; dismissing it surfaces the next-strongest (if any).

import { useEffect, useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  type ArrayMergeSuggestion,
  getArrayMergeSuggestions,
  mergeArrayInto,
  dismissArrayMergeSuggestion,
} from "../lib/api";

interface Props {
  /** The "anchor" array — the one whose card the operator is currently
   *  looking at. Suggestions are scored relative to this array; merging
   *  moves the duplicate's utility accounts INTO this array. */
  arrayId: number;
  arrayName: string;
  /** Bumped by parent on any UA/array mutation so we re-fetch. */
  refreshSignal?: number;
  /** Called after a successful merge so parent can reload its arrays. */
  onMerged?: (dstId: number, srcId: number) => void;
}

export function ArrayMergeSuggestionBanner({
  arrayId,
  arrayName,
  refreshSignal,
  onMerged,
}: Props) {
  const toast = useToast();
  const [suggestions, setSuggestions] = useState<ArrayMergeSuggestion[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [hiddenIds, setHiddenIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    getArrayMergeSuggestions(arrayId)
      .then((rows) => {
        if (!cancelled) setSuggestions(rows);
      })
      .catch(() => {
        // Best-effort feature — never disrupt the array list if the
        // suggestions endpoint errors.
        if (!cancelled) setSuggestions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [arrayId, refreshSignal]);

  if (!suggestions) return null;
  const visible = suggestions.filter((s) => !hiddenIds.has(s.id));
  if (visible.length === 0) return null;
  const top = visible[0];

  async function doMerge() {
    setBusy(top.id);
    try {
      // Merge the DUPLICATE (top.id) INTO this array (arrayId). The
      // duplicate's UAs come here, the duplicate row gets soft-deleted.
      const res = await mergeArrayInto(top.id, arrayId);
      const moved = res.reparented_utility_accounts;
      toast.success(
        moved > 0
          ? `Merged “${top.name}” into “${arrayName}” (+${moved} utility ${moved === 1 ? "account" : "accounts"}).`
          : `Merged “${top.name}” into “${arrayName}”.`,
      );
      setHiddenIds((prev) => new Set(prev).add(top.id));
      onMerged?.(arrayId, top.id);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't merge those arrays.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function doDismiss() {
    setBusy(top.id);
    try {
      await dismissArrayMergeSuggestion(arrayId, top.id);
      setHiddenIds((prev) => new Set(prev).add(top.id));
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't dismiss this suggestion.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
      <p className="text-sm font-semibold text-amber-900">
        Possible duplicate array: <span className="font-semibold">{top.name}</span>
        {top.nepool_gis_id && (
          <span className="ml-1.5 text-xs font-medium text-amber-700">
            (NEPOOL {top.nepool_gis_id})
          </span>
        )}
      </p>
      <p className="mt-0.5 text-xs text-amber-800">
        {top.reasons.length > 0
          ? `Match signals: ${top.reasons.join(", ")}.`
          : "These two look like the same array."}{" "}
        Merging moves all utility accounts under “{arrayName}” and removes the duplicate.
      </p>
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
            `Merge into “${arrayName}”`
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
