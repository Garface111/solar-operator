import { useEffect, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  nepoolPreview,
  nepoolCommit,
  type NepoolProposal,
  type NepoolAvailableArray,
} from "../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  onAssigned: () => void;
  /** When set, scopes the preview to this client's arrays only. */
  clientId?: number;
  clientName?: string;
}

type Stage = "upload" | "parsing" | "preview";

interface ProposalRow extends NepoolProposal {
  include: boolean;
}

interface ManualRow {
  extracted_name: string;
  extracted_nepool_gis_id: string;
  assigned_array_id: number | null;
}

const ACCEPT = ".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv";

function confidenceLabel(score: number): { label: string; className: string } {
  if (score >= 0.95) return { label: "High", className: "text-green-700" };
  if (score >= 0.85) return { label: "Likely", className: "text-primary-700" };
  return { label: "Possible", className: "text-amber-700" };
}

export function AssignNepoolFromSpreadsheetModal({ open, onClose, onAssigned, clientId, clientName }: Props) {
  const toast = useToast();
  const [stage, setStage] = useState<Stage>("upload");
  const [proposals, setProposals] = useState<ProposalRow[]>([]);
  const [manualRows, setManualRows] = useState<ManualRow[]>([]);
  const [availableArrays, setAvailableArrays] = useState<NepoolAvailableArray[]>([]);
  const [source, setSource] = useState<"gmcs_shape" | "llm" | "heuristic" | null>(null);
  const [skippedOverwrites, setSkippedOverwrites] = useState(0);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [committing, setCommitting] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (open) {
      setStage("upload");
      setProposals([]);
      setManualRows([]);
      setAvailableArrays([]);
      setSource(null);
      setSkippedOverwrites(0);
      setError(null);
      setDragOver(false);
      setCommitting(false);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !committing) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, committing, onClose]);

  if (!open) return null;

  async function handleFile(file: File) {
    setStage("parsing");
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const res = await nepoolPreview(file, controller.signal, clientId);
      if (controller.signal.aborted) return;
      if (!res.proposals.length && !res.unmatched_pairs.length) {
        setError(
          "We couldn't find any (array name, NEPOOL ID) pairs in that file. " +
          "Try a different file or enter IDs manually.",
        );
        setStage("upload");
        return;
      }
      setSource(res.source);
      setSkippedOverwrites(res.skipped_overwrites);
      // Default: checked if High (>=0.95) or Likely (>=0.85), unchecked if Possible.
      setProposals(
        res.proposals.map((p) => ({ ...p, include: p.match.confidence >= 0.85 })),
      );
      setManualRows(
        res.unmatched_pairs.map((u) => ({
          extracted_name: u.extracted_name,
          extracted_nepool_gis_id: u.extracted_nepool_gis_id,
          assigned_array_id: null,
        })),
      );
      setAvailableArrays(res.available_arrays);
      setStage("preview");
    } catch (err) {
      if (controller.signal.aborted) {
        setStage("upload");
        return;
      }
      setError(
        err instanceof Error
          ? err.message
          : "Couldn't parse that file. Try a different file.",
      );
      setStage("upload");
    } finally {
      abortRef.current = null;
    }
  }

  function cancelParsing() {
    abortRef.current?.abort();
    setStage("upload");
  }

  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (f) handleFile(f);
    e.target.value = "";
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) handleFile(f);
  }

  // Available arrays for the manual dropdown: exclude ones already in proposals.
  const usedArrayIds = new Set(proposals.map((p) => p.match.array_id));
  function dropdownOptions(currentArrayId: number | null): NepoolAvailableArray[] {
    return availableArrays.filter(
      (a) => !usedArrayIds.has(a.array_id) || a.array_id === currentArrayId,
    );
  }

  async function handleCommit() {
    const checkedProposals = proposals
      .filter((p) => p.include)
      .map((p) => ({ array_id: p.match.array_id, nepool_gis_id: p.extracted_nepool_gis_id }));
    const manualAssignments = manualRows
      .filter((r) => r.assigned_array_id !== null)
      .map((r) => ({ array_id: r.assigned_array_id!, nepool_gis_id: r.extracted_nepool_gis_id }));
    const assignments = [...checkedProposals, ...manualAssignments];

    if (!assignments.length || committing) return;
    setCommitting(true);
    try {
      const res = await nepoolCommit(assignments);
      const n = res.updated;
      toast.success(`Assigned NEPOOL IDs to ${n} array${n === 1 ? "" : "s"}`);
      if (res.errors.length) {
        toast.error(`${res.errors.length} assignment${res.errors.length === 1 ? "" : "s"} failed — check the list`);
      }
      onAssigned();
      onClose();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't save — try again");
    } finally {
      setCommitting(false);
    }
  }

  const checkedCount =
    proposals.filter((p) => p.include).length +
    manualRows.filter((r) => r.assigned_array_id !== null).length;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-zinc-900/40 px-4 py-8"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !committing) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Assign NEPOOL IDs from spreadsheet"
        className="flex max-h-full w-full max-w-3xl flex-col rounded-xl border border-zinc-200 bg-white p-6 shadow-xl"
      >
        <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
          {clientId !== undefined && clientName
            ? `Import NEPOOL IDs for ${clientName}`
            : "Find NEPOOL IDs in a spreadsheet"}
        </h2>

        {stage === "upload" && (
          <div className="mt-4">
            <p className="mb-3 text-sm text-zinc-600">
              {clientId !== undefined && clientName ? (
                <>
                  Drop a spreadsheet with {clientName}&apos;s array names + NEPOOL IDs.
                  We&apos;ll match against <strong>this client&apos;s arrays only</strong> and
                  ask before saving. We never create new arrays from this — only fill in missing IDs.
                </>
              ) : (
                <>
                  Drop any spreadsheet that has your array names + NEPOOL IDs. We&apos;ll
                  find the matches and ask before saving. We never create new arrays from
                  this — only fill in missing IDs.
                </>
              )}
            </p>
            <p className="mb-4 text-xs text-zinc-400">
              File contents are sent to an AI model to extract the data. No data
              is stored by the AI provider. Review all rows before saving.
            </p>
            <div
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={onDrop}
              onClick={() => fileInput.current?.click()}
              className={[
                "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors",
                dragOver
                  ? "border-primary-500 bg-primary-50"
                  : "border-zinc-300 bg-zinc-50 hover:border-zinc-400",
              ].join(" ")}
            >
              <span className="text-sm font-medium text-zinc-700">
                Drag &amp; drop a file here
              </span>
              <span className="text-xs text-zinc-500">or</span>
              <Button
                variant="secondary"
                type="button"
                onClick={(e) => { e.stopPropagation(); fileInput.current?.click(); }}
              >
                Choose file
              </Button>
              <span className="mt-1 text-xs text-zinc-400">.xlsx or .csv</span>
            </div>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={onPick}
            />
            {error && <p className="mt-4 text-sm text-red-600">{error}</p>}
            <div className="mt-6 flex justify-end">
              <Button variant="ghost" onClick={onClose}>Cancel</Button>
            </div>
          </div>
        )}

        {stage === "parsing" && (
          <div className="mt-4 flex flex-col items-center justify-center gap-4 py-16 text-sm text-zinc-500">
            <Spinner className="h-6 w-6 text-primary-500" />
            <span>Scanning your file for NEPOOL IDs…</span>
            <p className="text-xs text-zinc-400">
              AI extraction can take up to 2 minutes for large files.
            </p>
            <Button variant="ghost" onClick={cancelParsing}>Cancel</Button>
          </div>
        )}

        {stage === "preview" && (
          <div className="mt-4 flex min-h-0 flex-1 flex-col">
            {source === "heuristic" && (
              <div className="mb-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                We couldn&apos;t use AI to parse this file — guessed at patterns.
                Double-check every row before saving.
              </div>
            )}

            {skippedOverwrites > 0 && (
              <div className="mb-3 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm text-zinc-700">
                {skippedOverwrites} array{skippedOverwrites === 1 ? "" : "s"} already
                {skippedOverwrites === 1 ? " has" : " have"} a NEPOOL ID and{" "}
                {skippedOverwrites === 1 ? "was" : "were"} skipped. Edit each array
                individually to change it.
              </div>
            )}

            <details className="mb-3 rounded-xl border border-zinc-200 bg-zinc-50">
              <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium text-zinc-500 hover:text-zinc-700">
                What if this looks wrong?
              </summary>
              <p className="px-4 pb-3 pt-1 text-xs text-zinc-500">
                This is a preview — <strong>nothing has been saved yet</strong>.
                Cancel to discard and try a different file.
              </p>
            </details>

            <p className="mb-3 text-sm text-zinc-600">
              Review matches and uncheck any you want to skip.
            </p>

            <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-zinc-200">
              <table className="w-full border-collapse text-sm">
                <thead className="sticky top-0 bg-zinc-50 text-left text-xs font-medium text-zinc-500">
                  <tr>
                    <th className="w-8 px-2 py-2"></th>
                    <th className="px-2 py-2">Array (from your file)</th>
                    <th className="px-2 py-2">Match (in your account)</th>
                    <th className="px-2 py-2">NEPOOL ID</th>
                    <th className="px-2 py-2">Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {proposals.map((p, i) => {
                    const conf = confidenceLabel(p.match.confidence);
                    return (
                      <tr
                        key={`prop-${i}`}
                        className={[
                          "border-t border-zinc-100",
                          p.include ? "" : "opacity-40",
                        ].join(" ")}
                      >
                        <td className="px-2 py-1.5 align-middle">
                          <input
                            type="checkbox"
                            checked={p.include}
                            onChange={() =>
                              setProposals((ps) =>
                                ps.map((r, idx) => idx === i ? { ...r, include: !r.include } : r)
                              )
                            }
                            aria-label={`Include ${p.extracted_name}`}
                            className="h-4 w-4 accent-primary-500"
                          />
                        </td>
                        <td className="px-2 py-1.5 text-zinc-700">{p.extracted_name}</td>
                        <td className="px-2 py-1.5 text-zinc-800 font-medium">
                          ✓ {p.match.array_name}
                        </td>
                        <td className="px-2 py-1.5 font-mono text-zinc-800">
                          {p.extracted_nepool_gis_id}
                        </td>
                        <td className={`px-2 py-1.5 text-xs font-medium ${conf.className}`}>
                          {conf.label} ({(p.match.confidence * 100).toFixed(0)}%)
                        </td>
                      </tr>
                    );
                  })}

                  {manualRows.map((r, i) => {
                    const opts = dropdownOptions(r.assigned_array_id);
                    return (
                      <tr key={`unmatched-${i}`} className="border-t border-zinc-100 bg-zinc-50/50">
                        <td className="px-2 py-1.5 align-middle">
                          <input
                            type="checkbox"
                            checked={r.assigned_array_id !== null}
                            onChange={() =>
                              setManualRows((rs) =>
                                rs.map((row, idx) =>
                                  idx === i
                                    ? { ...row, assigned_array_id: row.assigned_array_id !== null ? null : (opts[0]?.array_id ?? null) }
                                    : row
                                )
                              )
                            }
                            disabled={opts.length === 0}
                            aria-label={`Include unmatched ${r.extracted_name}`}
                            className="h-4 w-4 accent-primary-500 disabled:opacity-40"
                          />
                        </td>
                        <td className="px-2 py-1.5 text-zinc-500 italic">{r.extracted_name}</td>
                        <td className="px-2 py-1.5">
                          {opts.length > 0 ? (
                            <select
                              value={r.assigned_array_id ?? ""}
                              onChange={(e) => {
                                const val = e.target.value ? Number(e.target.value) : null;
                                setManualRows((rs) =>
                                  rs.map((row, idx) => idx === i ? { ...row, assigned_array_id: val } : row)
                                );
                              }}
                              className="w-full rounded-md border border-zinc-300 bg-white px-2 py-1 text-sm text-zinc-700 focus:outline-none focus:ring-1 focus:ring-primary-400/40"
                            >
                              <option value="">— pick an array —</option>
                              {opts.map((a) => (
                                <option key={a.array_id} value={a.array_id}>
                                  {a.array_name}{a.client_name ? ` (${a.client_name})` : ""}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <span className="text-xs text-zinc-400">✗ no match</span>
                          )}
                        </td>
                        <td className="px-2 py-1.5 font-mono text-zinc-800">
                          {r.extracted_nepool_gis_id}
                        </td>
                        <td className="px-2 py-1.5 text-xs text-zinc-400">—</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="mt-6 flex items-center justify-between gap-2">
              <span className="text-xs text-zinc-500">
                {checkedCount} assignment{checkedCount === 1 ? "" : "s"} selected
              </span>
              <div className="flex gap-2">
                <Button variant="secondary" onClick={onClose} disabled={committing}>
                  Cancel
                </Button>
                <Button onClick={handleCommit} disabled={!checkedCount || committing}>
                  {committing ? (
                    <><Spinner /> Saving…</>
                  ) : (
                    `Assign ${checkedCount} NEPOOL ID${checkedCount === 1 ? "" : "s"}`
                  )}
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
