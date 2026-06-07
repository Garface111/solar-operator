import { useEffect, useRef, useState } from "react";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  ingestPreview,
  ingestCommit,
  listClients,
  type IngestRow,
  type IngestCommitResult,
  type ClientRow,
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

/** An editable preview row plus UI-only state. */
interface EditableRow extends IngestRow {
  include: boolean;
  collision_action: "skip" | "overwrite" | "new";
  /** How the frontend filled a blank operator_name (not from server). */
  _autofillKind?: "array_match" | "filename" | null;
}

type Stage = "upload" | "parsing" | "preview" | "result";

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
  const [importedLogins, setImportedLogins] = useState(0);
  const [importedClients, setImportedClients] = useState(0);
  const [existingClients, setExistingClients] = useState<ClientRow[]>([]);
  const [autoFilledCount, setAutoFilledCount] = useState(0);
  const [commitResult, setCommitResult] = useState<IngestCommitResult | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Load existing clients once when modal opens — used for auto-matching
  // blank client-name cells against arrays the operator already has.
  useEffect(() => {
    if (!open) return;
    listClients().then(setExistingClients).catch(() => setExistingClients([]));
  }, [open]);

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
      setImportedLogins(0);
      setImportedClients(0);
      setAutoFilledCount(0);
      setCommitResult(null);
    }
  }, [open]);

  // Auto-close the result step after 4 s.
  useEffect(() => {
    if (stage !== "result") return;
    const timer = setTimeout(() => onClose(), 4000);
    return () => clearTimeout(timer);
  }, [stage, onClose]);

  // Close on Escape (matches the shared Modal behavior).
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !committing && stage !== "result") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, committing, stage, onClose]);

  if (!open) return null;

  async function handleFile(file: File) {
    setStage("parsing");
    setError(null);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const res = await ingestPreview(file, controller.signal);
      if (controller.signal.aborted) return;

      // If the server says the file is empty, stay on upload and show message.
      const emptyWarn = (res.warnings ?? []).find((w) => w.kind === "empty_file");
      if (emptyWarn || !res.arrays.length) {
        setError(
          emptyWarn?.message ??
          "We couldn't find any arrays in that file. Try a different file, or add clients manually.",
        );
        setStage("upload");
        return;
      }

      setSource(res.source);

      // Smart-fill blank client names BEFORE first paint:
      //  1. If row collides with an existing array, suggest the closest-named client.
      //  2. If still blank, fall back to a name derived from the filename stem.
      const fileStem = deriveClientNameFromFilename(file.name);
      let filled = 0;
      const smartRows = res.arrays.map((r): EditableRow => {
        const hasNepolCollision = !!r.provenance?.nepool_collision;
        const baseAction: "skip" | "overwrite" | "new" = hasNepolCollision ? "skip" : "new";

        if ((r.operator_name ?? "").trim()) {
          return { ...r, include: true, collision_action: baseAction, _autofillKind: null };
        }
        // Try fuzzy match against existing client names using array name.
        const guess = guessClientFromArray(r.array_name ?? "", existingClients);
        if (guess) {
          filled += 1;
          return { ...r, operator_name: guess, include: true, collision_action: baseAction, _autofillKind: "array_match" };
        }
        if (fileStem) {
          filled += 1;
          return { ...r, operator_name: fileStem, include: true, collision_action: baseAction, _autofillKind: "filename" };
        }
        return { ...r, include: true, collision_action: baseAction, _autofillKind: null };
      });
      setRows(smartRows);
      setAutoFilledCount(filled);
      setImportedLogins(res.imported_logins ?? 0);
      setImportedClients(res.imported_clients ?? 0);
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

  function setCollisionAction(i: number, action: "skip" | "overwrite" | "new") {
    setRows((rs) =>
      rs.map((r, idx) => {
        if (idx !== i) return r;
        // When user picks "skip", auto-uncheck the row too.
        return { ...r, collision_action: action, include: action !== "skip" };
      }),
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
      // Strip UI-only fields before sending.
      const payload: IngestRow[] = selected.map((r) => {
        // eslint-disable-next-line @typescript-eslint/no-unused-vars
        const { include: _i, _autofillKind: _ak, provenance: _p, ...rest } = r;
        return rest as IngestRow;
      });
      const res = await ingestCommit(payload, forceClientId);
      onImported(); // Refresh parent list before showing result step.
      setCommitResult(res);
      setStage("result");
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't import — try again",
      );
    } finally {
      setCommitting(false);
    }
  }

  // Derived counts for warnings summary bar.
  const nepoolCollisionRows = rows.filter((r) => r.provenance?.nepool_collision);
  const fuzzyClientRows = rows.filter(
    (r) => r.provenance?.client_match?.match_kind === "fuzzy",
  );
  const lowConfCount = source === "llm"
    ? rows.filter((r) => {
        const c = r.provenance?.confidence;
        return c !== null && c !== undefined && c < 0.85;
      }).length
    : 0;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-zinc-900/40 px-4 py-8"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !committing && stage !== "result") onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Import arrays from spreadsheet"
        className="flex max-h-full w-full max-w-3xl flex-col rounded-xl border border-zinc-200 bg-white p-6 shadow-xl"
      >
        <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
          Import arrays from spreadsheet — we&apos;ll review before saving
        </h2>

        {/* ── Upload stage ─────────────────────────────────────────────── */}
        {stage === "upload" && (
          <div className="mt-4">
            <p className="mb-3 text-sm text-zinc-600">
              Drop any spreadsheet. We&apos;ll scan every sheet for clients,
              utility logins, accounts, arrays, and NEPOOL-GIS IDs — and link
              them automatically. Review everything before anything is saved.
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

        {/* ── Parsing stage ─────────────────────────────────────────────── */}
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

        {/* ── Preview stage ─────────────────────────────────────────────── */}
        {stage === "preview" && (
          <div className="mt-4 flex min-h-0 flex-1 flex-col">

            {/* Parse-source banner */}
            {source === "llm" && (
              <div className="mb-3 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm">
                <p className="font-medium text-zinc-800">
                  Parsed by AI
                  <span
                    title="We use AI to parse free-form spreadsheets. Low confidence = the AI wasn't sure. Always review before saving."
                    className="ml-1.5 cursor-help text-zinc-400"
                  >ⓘ</span>
                </p>
                <p className="mt-0.5 text-zinc-600">
                  {rows.length} row{rows.length !== 1 ? "s" : ""} extracted
                  {lowConfCount > 0 && (
                    <> · <span className="text-amber-700">{lowConfCount} had low confidence (highlighted)</span></>
                  )}
                </p>
              </div>
            )}

            {source === "gmcs_shape" && (
              <div className="mb-3 rounded-xl border border-primary-200 bg-primary-50 px-4 py-3 text-sm text-primary-900">
                <p className="font-medium">Recognized GMCS-format workbook</p>
                <p className="mt-0.5 text-primary-800">
                  Pulled one row per sheet, with the array name and NEPOOL-GIS ID
                  from the sheet title. Set the client below — it applies to all rows.
                </p>
                <div className="mt-3">
                  <label className="block text-xs font-medium text-primary-800 mb-1">
                    Client (all arrays belong to:)
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

            {source === "heuristic" && (
              <div className="mb-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                Best-effort column parse — AI extraction wasn&apos;t available.
                Double-check every row before saving.
              </div>
            )}

            {/* Top-level collision summaries */}
            {fuzzyClientRows.length > 0 && (
              <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
                {fuzzyClientRows.length} row{fuzzyClientRows.length !== 1 ? "s" : ""} look like new clients but are similar to an existing one — review the highlighted rows.
              </div>
            )}
            {nepoolCollisionRows.length > 0 && (
              <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
                {nepoolCollisionRows.length} row{nepoolCollisionRows.length !== 1 ? "s" : ""} would create duplicate NEPOOL IDs — choose Skip / Overwrite / New per row.
              </div>
            )}

            {/* Detected logins / clients from hierarchical extraction */}
            {importedLogins > 0 && (
              <div className="mb-3 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-2.5 text-sm text-zinc-700">
                Found {importedLogins} utility login{importedLogins === 1 ? "" : "s"} across {importedClients} client{importedClients === 1 ? "" : "s"}.
              </div>
            )}

            {/* Auto-fill banner */}
            {autoFilledCount > 0 && (
              <div className="mb-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2.5 text-sm text-emerald-600">
                ✨ Auto-matched {autoFilledCount} row{autoFilledCount === 1 ? "" : "s"} to a client (existing match or derived from filename). Override below if any are wrong.
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
                    <th className="px-2 py-2">Client</th>
                    <th className="px-2 py-2">Array</th>
                    <th className="px-2 py-2">NEPOOL ID</th>
                    <th className="px-2 py-2">Utility account</th>
                    <th className="px-2 py-2">Notes</th>
                    <th className="px-2 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    const prov = r.provenance;
                    const isLowConf = source === "llm" && prov?.confidence != null && prov.confidence < 0.85;
                    const hasNepoolCollision = !!prov?.nepool_collision;
                    return (
                      <tr
                        key={i}
                        className={[
                          "border-t border-zinc-100",
                          r.include ? "" : "opacity-40",
                          isLowConf ? "bg-yellow-50/50" : "",
                          hasNepoolCollision ? "bg-amber-50" : "",
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
                          <input
                            list="import-client-suggestions"
                            value={r.operator_name ?? ""}
                            placeholder="(blank — will create new client)"
                            onChange={(e) => editRow(i, "operator_name", e.target.value)}
                            className={[
                              "w-full rounded-md border bg-transparent px-1.5 py-1 text-sm placeholder:text-zinc-300 hover:border-zinc-200 focus:bg-white focus:outline-none focus:ring-1",
                              !r.operator_name
                                ? "border-amber-300 text-amber-800 focus:border-amber-400 focus:ring-amber-400/40"
                                : isExistingClient(r.operator_name, existingClients)
                                  ? "border-emerald-300 text-emerald-600 focus:border-emerald-400 focus:ring-emerald-400/40"
                                  : "border-transparent text-zinc-800 focus:border-primary-400 focus:ring-primary-400/40",
                            ].join(" ")}
                          />
                          {/* Per-row client provenance pill */}
                          <ClientProvenancePill row={r} onUseExisting={(name) => editRow(i, "operator_name", name)} />
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
                        <td className="px-2 py-1 align-top text-xs">
                          {/* Source/confidence pill */}
                          <SourcePill row={r} source={source} />
                          {/* NEPOOL collision — dropdown + info */}
                          {hasNepoolCollision && (
                            <div className="mt-1">
                              <p className="whitespace-nowrap text-amber-800">
                                NEPOOL {r.nepool_gis_id} already on{" "}
                                <span className="font-medium">
                                  {prov!.nepool_collision!.existing_client_name} / {prov!.nepool_collision!.existing_array_name}
                                </span>
                              </p>
                              <select
                                value={r.collision_action ?? "skip"}
                                onChange={(e) =>
                                  setCollisionAction(i, e.target.value as "skip" | "overwrite" | "new")
                                }
                                className="mt-1 rounded border border-amber-300 bg-white px-1 py-0.5 text-xs text-amber-900 focus:outline-none"
                              >
                                <option value="skip">Skip this row</option>
                                <option value="overwrite">Overwrite existing array</option>
                                <option value="new">Create as new array</option>
                              </select>
                            </div>
                          )}
                          {/* Legacy name-collision indicator */}
                          {r.collision && !hasNepoolCollision && (
                            <span
                              title={
                                r.collision === "both"
                                  ? "Client and array name already exist — will be merged"
                                  : r.collision === "client"
                                    ? "Client name already exists — will be merged"
                                    : "Array name already exists — will be merged"
                              }
                              className="whitespace-nowrap text-amber-700"
                            >
                              ⚠ Collides — will merge
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {/* Datalist suggestions for the Client column inputs */}
            <datalist id="import-client-suggestions">
              {existingClients.map((c) => (
                <option key={c.id} value={c.name} />
              ))}
            </datalist>
            <div className="mt-6 flex items-center justify-between gap-2">
              <span className="text-xs text-zinc-500">
                {selected.length} array{selected.length === 1 ? "" : "s"} across {clientCount} client{clientCount === 1 ? "" : "s"} selected
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
                    `Import ${selected.length} array${selected.length === 1 ? "" : "s"}`
                  )}
                </Button>
              </div>
            </div>
          </div>
        )}

        {/* ── Result stage ──────────────────────────────────────────────── */}
        {stage === "result" && commitResult && (
          <div className="mt-6 flex flex-col items-center gap-4 py-8 text-center">
            <div className="text-4xl text-emerald-500">✓</div>
            <p className="text-base font-medium text-zinc-900">
              {forceClientId
                ? `Imported ${commitResult.arrays_created} array${commitResult.arrays_created === 1 ? "" : "s"}${forceClientName ? ` into ${forceClientName}` : ""}.`
                : `Imported ${commitResult.arrays_created} array${commitResult.arrays_created === 1 ? "" : "s"} across ${commitResult.clients_created} client${commitResult.clients_created === 1 ? "" : "s"}.`
              }
              {(commitResult.skipped_count ?? 0) > 0 && (
                <span className="ml-1 text-zinc-500">
                  {commitResult.skipped_count} skipped (you chose to).
                </span>
              )}
            </p>
            <div className="flex gap-3">
              <Button onClick={onClose}>Done</Button>
              <Button
                variant="secondary"
                onClick={() => {
                  onClose();
                  window.location.href = "/accounts";
                }}
              >
                View arrays →
              </Button>
            </div>
            <p className="text-xs text-zinc-400">Auto-closing in a few seconds…</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── helper sub-components ────────────────────────────────────────────────────

/** Tiny pill showing per-row AI/GMCS/heuristic source + confidence signal. */
function SourcePill({
  row,
  source,
}: {
  row: EditableRow;
  source: "llm" | "heuristic" | "gmcs_shape" | null;
}) {
  const prov = row.provenance;
  if (!prov && !source) return null;
  const s = prov?.source ?? source;
  if (s === "gmcs" || source === "gmcs_shape") {
    return (
      <span className="inline-block rounded bg-primary-100 px-1.5 py-0.5 text-xs text-primary-800 whitespace-nowrap">
        📄 GMCS
      </span>
    );
  }
  if (s === "heuristic" || source === "heuristic") {
    return (
      <span className="inline-block rounded bg-zinc-100 px-1.5 py-0.5 text-xs text-zinc-600 whitespace-nowrap">
        🔍 column-guess
      </span>
    );
  }
  if (s === "llm" || source === "llm") {
    const conf = prov?.confidence;
    const isLow = conf !== null && conf !== undefined && conf < 0.85;
    return (
      <span
        className={[
          "inline-block rounded px-1.5 py-0.5 text-xs whitespace-nowrap",
          isLow
            ? "bg-yellow-100 text-yellow-800"
            : "bg-zinc-100 text-zinc-600",
        ].join(" ")}
        title={conf != null ? `AI confidence: ${Math.round(conf * 100)}%` : undefined}
      >
        🤖 AI{isLow ? " · low" : ""}
      </span>
    );
  }
  return null;
}

/** Pill shown below the client name input indicating how the name was determined. */
function ClientProvenancePill({
  row,
  onUseExisting,
}: {
  row: EditableRow;
  onUseExisting: (name: string) => void;
}) {
  const prov = row.provenance;
  const match = prov?.client_match;

  if (match?.match_kind === "exact") {
    return (
      <p className="mt-0.5 text-xs text-emerald-600">
        ✓ matches your client &lsquo;{match.client_name}&rsquo;
      </p>
    );
  }
  if (match?.match_kind === "fuzzy") {
    return (
      <p className="mt-0.5 text-xs text-amber-700">
        ≈ similar to &lsquo;{match.client_name}&rsquo;{" "}
        <button
          type="button"
          onClick={() => onUseExisting(match.client_name)}
          className="underline hover:text-amber-900"
        >
          use existing
        </button>
      </p>
    );
  }
  // Frontend autofill indicators (when server didn't report a server-side match).
  if (row._autofillKind === "filename") {
    return (
      <p className="mt-0.5 text-xs text-zinc-400">✨ auto-filled from filename</p>
    );
  }
  if (row._autofillKind === "array_match") {
    return (
      <p className="mt-0.5 text-xs text-zinc-400">✨ matched from array name</p>
    );
  }
  return null;
}

/** Lowercase substring containment for the "did this row's client name
 *  already exist in our roster?" cell-color signal. */
function isExistingClient(name: string | null | undefined, clients: ClientRow[]): boolean {
  const n = (name ?? "").trim().toLowerCase();
  if (!n) return false;
  return clients.some((c) => c.name.trim().toLowerCase() === n);
}

/** Derive a client name from an uploaded filename. "Bruce Genereaux 2026.xlsx"
 *  → "Bruce Genereaux". Strips extension, common year/quarter suffixes, and
 *  the words "report"/"roster"/"arrays" that operators title spreadsheets with. */
function deriveClientNameFromFilename(filename: string): string {
  let stem = filename.replace(/\.[^.]+$/, "");
  // Strip trailing year (2020-2099), quarter (Q1-Q4), and common report words
  stem = stem.replace(/[_\-\s]*(20\d{2}|q[1-4]|quarterly|annual|report|roster|arrays?|nepool|gmcs)[_\-\s]*/gi, " ");
  stem = stem.replace(/[_\-]+/g, " ").replace(/\s+/g, " ").trim();
  // Title-case naive
  if (!stem) return "";
  return stem.replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Fuzzy match an array name against existing client names. Useful when
 *  the spreadsheet labels rows by array but doesn't name the client — if
 *  a client's name appears as a substring of the array (e.g. "Bruce
 *  Genereaux - Maple Ridge"), we can confidently auto-fill. */
function guessClientFromArray(arrayName: string, clients: ClientRow[]): string | null {
  const a = arrayName.toLowerCase();
  if (!a) return null;
  // Prefer longer client names (more specific) over short ones.
  const sorted = [...clients].sort((x, y) => y.name.length - x.name.length);
  for (const c of sorted) {
    const n = c.name.trim().toLowerCase();
    if (n.length < 3) continue; // too short to be a confident match
    if (a.includes(n)) return c.name;
  }
  return null;
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
