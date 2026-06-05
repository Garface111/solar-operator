import { useEffect, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  ingestPreview,
  ingestCommit,
  type IngestRow,
} from "../lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Called after a successful commit so the parent can refresh its list. */
  onImported: () => void;
  /** When set, pin every imported row to this Client (operator_name in
   *  the spreadsheet is ignored on the backend). Used by the per-client
   *  "Import arrays into this client" button. */
  forceClientId?: number;
  /** Optional display name for the pinned client, surfaced in copy. */
  forceClientName?: string;
}

/** An editable preview row plus whether it's selected for import. */
interface EditableRow extends IngestRow {
  include: boolean;
}

type Stage = "upload" | "parsing" | "preview";

const ACCEPT = ".xlsx,.csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv";

export function ImportSpreadsheetModal({ open, onClose, onImported, forceClientId, forceClientName }: Props) {
  const toast = useToast();
  const [stage, setStage] = useState<Stage>("upload");
  const [rows, setRows] = useState<EditableRow[]>([]);
  const [source, setSource] = useState<"llm" | "heuristic" | "gmcs_shape" | null>(null);
  const [gmcsOperator, setGmcsOperator] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [committing, setCommitting] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Reset to a clean slate whenever the modal is (re)opened.
  useEffect(() => {
    if (open) {
      setStage("upload");
      setRows([]);
      setSource(null);
      setGmcsOperator("");
      setError(null);
      setDragOver(false);
      setCommitting(false);
    }
  }, [open]);

  // Close on Escape (matches the shared Modal behavior).
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
      const res = await ingestPreview(file, controller.signal);
      if (controller.signal.aborted) return;
      if (!res.arrays.length) {
        setError(
          "We couldn't find any arrays in that file. Try a different file, or add clients manually.",
        );
        setStage("upload");
        return;
      }
      setSource(res.source);
      setRows(res.arrays.map((r) => ({ ...r, include: true })));
      setStage("preview");
    } catch (err) {
      if (controller.signal.aborted) {
        setStage("upload");
        return;
      }
      setError(
        err instanceof Error
          ? err.message
          : "Couldn't parse that file. Try a different file, or add manually.",
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
    e.target.value = ""; // allow re-picking the same file
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) handleFile(f);
  }

  function editRow(i: number, field: keyof IngestRow, value: string) {
    setRows((rs) =>
      rs.map((r, idx) => (idx === i ? { ...r, [field]: value || null } : r)),
    );
  }

  function toggleRow(i: number) {
    setRows((rs) =>
      rs.map((r, idx) => (idx === i ? { ...r, include: !r.include } : r)),
    );
  }

  // When the GMCS global operator name changes, apply it to all selected rows.
  function applyGmcsOperator(name: string) {
    setGmcsOperator(name);
    setRows((rs) => rs.map((r) => ({ ...r, operator_name: name || null })));
  }

  const selected = rows.filter((r) => r.include);
  const clientCount = new Set(
    selected.map((r) => (r.operator_name || "Unassigned").trim().toLowerCase()),
  ).size;

  async function handleCommit() {
    if (!selected.length || committing) return;
    setCommitting(true);
    try {
      const res = await ingestCommit(
        selected.map(({ include: _include, ...r }) => r),
        forceClientId,
      );
      toast.success(
        forceClientId
          ? `Imported ${res.arrays_created} array${res.arrays_created === 1 ? "" : "s"}` +
            (forceClientName ? ` into ${forceClientName}` : "")
          : `Imported ${res.arrays_created} array${res.arrays_created === 1 ? "" : "s"}` +
            (res.clients_created
              ? ` under ${res.clients_created} new client${res.clients_created === 1 ? "" : "s"}`
              : ""),
      );
      onImported();
      onClose();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't import — try again",
      );
    } finally {
      setCommitting(false);
    }
  }

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
        aria-label="Import from spreadsheet"
        className="flex max-h-full w-full max-w-3xl flex-col rounded-xl border border-zinc-200 bg-white p-6 shadow-xl"
      >
        <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
          Import from spreadsheet
        </h2>

        {stage === "upload" && (
          <div className="mt-4">
            <p className="mb-3 text-sm text-zinc-600">
              Drop your roster of operators, arrays, and NEPOOL-GIS IDs. We&apos;ll
              read it and let you review everything before anything is saved.
            </p>
            <p className="mb-4 text-xs text-zinc-400">
              File contents are sent to an AI model to extract the row data. No data
              is stored by the AI provider. Review all rows before importing.
            </p>
            <div
              onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
              }}
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
                onClick={(e) => {
                  e.stopPropagation();
                  fileInput.current?.click();
                }}
              >
                Choose file
              </Button>
              <span className="mt-1 text-xs text-zinc-400">
                .xlsx or .csv
              </span>
            </div>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPT}
              className="hidden"
              onChange={onPick}
            />
            {error && (
              <p className="mt-4 text-sm text-red-600">{error}</p>
            )}
            <div className="mt-6 flex justify-end">
              <Button variant="ghost" onClick={onClose}>
                Cancel
              </Button>
            </div>
          </div>
        )}

        {stage === "parsing" && (
          <div className="mt-4 flex flex-col items-center justify-center gap-4 py-16 text-sm text-zinc-500">
            <Spinner className="h-6 w-6 text-primary-500" />
            <span>Parsing your spreadsheet…</span>
            <p className="text-xs text-zinc-400">
              AI extraction can take up to 2 minutes for large files.
            </p>
            <Button variant="ghost" onClick={cancelParsing}>
              Cancel
            </Button>
          </div>
        )}

        {stage === "preview" && (
          <div className="mt-4 flex min-h-0 flex-1 flex-col">
            {/* GMCS-shape notice */}
            {source === "gmcs_shape" && (
              <div className="mb-3 rounded-xl border border-primary-200 bg-primary-50 px-4 py-3 text-sm text-primary-900">
                <p className="font-medium">Detected GMCS-format workbook</p>
                <p className="mt-0.5 text-primary-800">
                  Pulled one row per sheet, with the array name and NEPOOL-GIS ID
                  from the sheet title. Set the owner below — it applies to all rows.
                </p>
                <div className="mt-3">
                  <label className="block text-xs font-medium text-primary-800 mb-1">
                    Owner / operator (all arrays belong to:)
                  </label>
                  <input
                    type="text"
                    value={gmcsOperator}
                    onChange={(e) => applyGmcsOperator(e.target.value)}
                    placeholder="e.g. Green Mountain Community Solar"
                    className="w-full rounded-lg border border-primary-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
                  />
                </div>
              </div>
            )}

            {/* Heuristic warning */}
            {source === "heuristic" && (
              <div className="mb-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                We couldn&apos;t use AI to parse this file — guessed at column
                meanings. Double-check every row before saving.
              </div>
            )}

            {/* Preview-is-not-saved note */}
            <details className="mb-3 rounded-xl border border-zinc-200 bg-zinc-50">
              <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium text-zinc-500 hover:text-zinc-700">
                What if this looks wrong?
              </summary>
              <p className="px-4 pb-3 pt-1 text-xs text-zinc-500">
                This is a preview — <strong>nothing has been saved yet</strong>.
                Cancel to discard and try a different file, or edit rows
                individually below before importing.
              </p>
            </details>

            <p className="mb-3 text-sm text-zinc-600">
              Review and edit before importing. Uncheck any row you want to skip.
            </p>
            <div className="min-h-0 flex-1 overflow-auto rounded-xl border border-zinc-200">
              <table className="w-full border-collapse text-sm">
                <thead className="sticky top-0 bg-zinc-50 text-left text-xs font-medium text-zinc-500">
                  <tr>
                    <th className="w-8 px-2 py-2"></th>
                    <th className="px-2 py-2">Operator</th>
                    <th className="px-2 py-2">Array</th>
                    <th className="px-2 py-2">NEPOOL ID</th>
                    <th className="px-2 py-2">Utility account</th>
                    <th className="px-2 py-2">Notes</th>
                    <th className="px-2 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr
                      key={i}
                      className={[
                        "border-t border-zinc-100",
                        r.include ? "" : "opacity-40",
                      ].join(" ")}
                    >
                      <td className="px-2 py-1 align-middle">
                        <input
                          type="checkbox"
                          checked={r.include}
                          onChange={() => toggleRow(i)}
                          aria-label={`Include row ${i + 1}`}
                          className="h-4 w-4 accent-primary-500"
                        />
                      </td>
                      <td className="px-1 py-1">
                        <div className="relative">
                          <input
                            value={r.operator_name ?? ""}
                            placeholder="(blank — will create Unassigned client)"
                            onChange={(e) => editRow(i, "operator_name", e.target.value)}
                            className={[
                              "w-full rounded-md border bg-transparent px-1.5 py-1 text-sm placeholder:text-zinc-300 hover:border-zinc-200 focus:bg-white focus:outline-none focus:ring-1",
                              !r.operator_name
                                ? "border-amber-300 text-amber-800 focus:border-amber-400 focus:ring-amber-400/40"
                                : "border-transparent text-zinc-800 focus:border-primary-400 focus:ring-primary-400/40",
                            ].join(" ")}
                          />
                        </div>
                      </td>
                      <CellInput
                        value={r.array_name}
                        onChange={(v) => editRow(i, "array_name", v)}
                        placeholder="(array name)"
                      />
                      <CellInput
                        value={r.nepool_gis_id}
                        onChange={(v) => editRow(i, "nepool_gis_id", v)}
                        placeholder="—"
                      />
                      <CellInput
                        value={r.gmp_account_number}
                        onChange={(v) => editRow(i, "gmp_account_number", v)}
                        placeholder="—"
                      />
                      <CellInput
                        value={r.notes}
                        onChange={(v) => editRow(i, "notes", v)}
                        placeholder="—"
                      />
                      <td className="px-2 py-1 align-middle text-xs">
                        {r.collision && (
                          <span
                            title={
                              r.collision === "both"
                                ? "Operator and array name already exist — will be merged"
                                : r.collision === "client"
                                  ? "Operator name already exists — will be merged"
                                  : "Array name already exists — will be merged"
                            }
                            className="whitespace-nowrap text-amber-700"
                          >
                            ⚠ Collides — will merge
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-6 flex items-center justify-between gap-2">
              <span className="text-xs text-zinc-500">
                {selected.length} array{selected.length === 1 ? "" : "s"} selected
              </span>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  onClick={onClose}
                  disabled={committing}
                >
                  Cancel
                </Button>
                <Button
                  onClick={handleCommit}
                  disabled={!selected.length || committing}
                >
                  {committing ? (
                    <>
                      <Spinner />
                      Importing…
                    </>
                  ) : (
                    `Import ${selected.length} array${selected.length === 1 ? "" : "s"} under ${clientCount} client${clientCount === 1 ? "" : "s"}`
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

/** A borderless, inline-editable table cell. */
function CellInput({
  value,
  onChange,
  placeholder,
}: {
  value: string | null;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <td className="px-1 py-1">
      <input
        value={value ?? ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-transparent bg-transparent px-1.5 py-1 text-sm text-zinc-800 placeholder:text-zinc-300 hover:border-zinc-200 focus:border-primary-400 focus:bg-white focus:outline-none focus:ring-1 focus:ring-primary-400/40"
      />
    </td>
  );
}
