import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import * as XLSX from "xlsx";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  type ClientRow,
  type VerificationCheck,
  listClients,
  uploadVerification,
  resolveVerification,
  fetchVerificationUploadedFile,
  fetchVerificationSoWorkbook,
} from "../lib/api";

// ─── period helpers ───────────────────────────────────────────────────────

function recentQuarters(): string[] {
  const now = new Date();
  const q = Math.floor(now.getMonth() / 3) + 1;
  let cq = q - 1;
  let cy = now.getFullYear();
  if (cq === 0) {
    cq = 4;
    cy -= 1;
  }
  const out: string[] = [];
  for (let i = 0; i < 4; i++) {
    out.push(`Q${cq} ${cy}`);
    cq -= 1;
    if (cq === 0) {
      cq = 4;
      cy -= 1;
    }
  }
  return out;
}

// ─── SheetJS rendering ────────────────────────────────────────────────────

async function xlsxToHtml(ab: ArrayBuffer): Promise<string> {
  const wb = XLSX.read(ab, { type: "array" });
  const ws = wb.Sheets[wb.SheetNames[0]];
  return XLSX.utils.sheet_to_html(ws, { id: "so-sheet-table" });
}

// ─── file panel ───────────────────────────────────────────────────────────

function FilePanel({
  check,
  label,
  blobUrl,
  sheetHtml,
  error,
  loading,
}: {
  check: VerificationCheck | null;
  label: string;
  blobUrl: string | null;
  sheetHtml: string | null;
  error: string | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="flex h-full min-h-[320px] items-center justify-center text-zinc-400">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-xl border-2 border-amber-300 bg-amber-50 p-5">
        <p className="text-sm font-semibold text-amber-800">Could not load — {label}</p>
        <p className="mt-1 text-sm text-amber-700">{error}</p>
      </div>
    );
  }

  if (!check || !blobUrl) {
    return (
      <div className="flex h-full min-h-[320px] items-center justify-center rounded-xl border-2 border-dashed border-zinc-200 text-sm text-zinc-400">
        Upload a file to see {label}
      </div>
    );
  }

  const mime = check.uploaded_mime;

  if (sheetHtml) {
    return (
      <div
        className="overflow-auto rounded-xl border border-zinc-200 bg-white p-2 text-xs"
        style={{ maxHeight: 520 }}
        dangerouslySetInnerHTML={{ __html: sheetHtml }}
      />
    );
  }

  if (mime === "application/pdf") {
    return (
      <iframe
        src={blobUrl}
        title={label}
        className="h-[520px] w-full rounded-xl border border-zinc-200 bg-white"
      />
    );
  }

  if (mime.startsWith("image/")) {
    return (
      <img
        src={blobUrl}
        alt={label}
        className="max-h-[520px] w-full rounded-xl border border-zinc-200 bg-white object-contain"
      />
    );
  }

  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-zinc-200 bg-white p-8">
      <p className="text-sm text-zinc-500">Preview not available for this file type.</p>
      <a
        href={blobUrl}
        download={check.uploaded_filename}
        className="text-sm font-medium text-primary-600 hover:underline"
      >
        Download {check.uploaded_filename}
      </a>
    </div>
  );
}

// ─── main screen ─────────────────────────────────────────────────────────────

export default function VerifyAccuracy() {
  const { clientId } = useParams<{ clientId: string }>();
  const cid = clientId ? Number(clientId) : NaN;
  const navigate = useNavigate();
  const toast = useToast();

  const quarters = recentQuarters();
  const [period, setPeriod] = useState(quarters[0] ?? "");
  const [client, setClient] = useState<ClientRow | null>(null);
  const [uploading, setUploading] = useState(false);
  const [check, setCheck] = useState<VerificationCheck | null>(null);

  // Left panel (uploaded file)
  const [uploadedBlobUrl, setUploadedBlobUrl] = useState<string | null>(null);
  const [uploadedHtml, setUploadedHtml] = useState<string | null>(null);
  const [uploadedLoading, setUploadedLoading] = useState(false);
  const [uploadedError, setUploadedError] = useState<string | null>(null);

  // Right panel (SO workbook)
  const [soHtml, setSoHtml] = useState<string | null>(null);
  const [soBlobUrl, setSoBlobUrl] = useState<string | null>(null);
  const [soLoading, setSoLoading] = useState(false);
  const [soError, setSoError] = useState<string | null>(null);

  // Resolve
  const [flagMode, setFlagMode] = useState(false);
  const [flagNote, setFlagNote] = useState("");
  const [resolving, setResolving] = useState(false);

  // Drag state
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const blobUrlsRef = useRef<string[]>([]);

  // Clean up blob URLs on unmount
  useEffect(() => {
    return () => {
      blobUrlsRef.current.forEach((u) => URL.revokeObjectURL(u));
    };
  }, []);

  function trackBlob(url: string) {
    blobUrlsRef.current.push(url);
    return url;
  }

  // Load client name
  useEffect(() => {
    if (isNaN(cid)) return;
    listClients()
      .then((clients) => {
        const found = clients.find((c) => c.id === cid) ?? null;
        setClient(found);
      })
      .catch(() => {});
  }, [cid]);

  async function loadPanels(newCheck: VerificationCheck) {
    setCheck(newCheck);

    // Load left panel (uploaded file)
    setUploadedLoading(true);
    setUploadedError(null);
    setUploadedBlobUrl(null);
    setUploadedHtml(null);
    try {
      const blob = await fetchVerificationUploadedFile(newCheck.id);
      const url = trackBlob(URL.createObjectURL(blob));
      setUploadedBlobUrl(url);

      const mime = newCheck.uploaded_mime;
      if (
        mime.includes("spreadsheet") ||
        mime.includes("excel") ||
        mime === "text/csv"
      ) {
        const ab = await blob.arrayBuffer();
        const html = await xlsxToHtml(ab);
        setUploadedHtml(html);
      }
    } catch (err) {
      setUploadedError(err instanceof Error ? err.message : "Failed to load uploaded file");
    } finally {
      setUploadedLoading(false);
    }

    // Load right panel (SO workbook)
    setSoLoading(true);
    setSoError(null);
    setSoHtml(null);
    setSoBlobUrl(null);
    try {
      const ab = await fetchVerificationSoWorkbook(newCheck.id);
      const html = await xlsxToHtml(ab);
      setSoHtml(html);
      const blob = new Blob([ab], {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      });
      setSoBlobUrl(trackBlob(URL.createObjectURL(blob)));
    } catch (err) {
      setSoError(err instanceof Error ? err.message : "Failed to generate SO workbook");
    } finally {
      setSoLoading(false);
    }
  }

  async function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const file = files[0]!;
    setUploading(true);
    try {
      const result = await uploadVerification(cid, period, file);
      await loadPanels(result);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setIsDragging(false);
      handleFiles(e.dataTransfer.files);
    },
    [period, cid],
  );

  async function handleResolve(status: "confirmed" | "flagged") {
    if (!check) return;
    setResolving(true);
    try {
      await resolveVerification(check.id, status, flagNote || undefined);
      toast.success(status === "confirmed" ? "Marked as confirmed" : "Flagged for review");
      navigate("/clients");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't save result");
    } finally {
      setResolving(false);
    }
  }

  if (isNaN(cid)) {
    return (
      <div className="p-8 text-sm text-zinc-500">Invalid client ID.</div>
    );
  }

  const clientName = client?.name ?? `Client ${cid}`;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => navigate("/clients")}
          className="rounded-lg p-1.5 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
          aria-label="Back to clients"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <polyline points="10,2 4,8 10,14" />
          </svg>
        </button>
        <div>
          <h1 className="text-lg font-semibold text-zinc-900">Verify accuracy</h1>
          <p className="text-sm text-zinc-500">{clientName}</p>
        </div>
      </div>

      {/* Period selector + dropzone */}
      {!check && (
        <div className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm">
          <div className="mb-5 flex flex-wrap items-end gap-4">
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-zinc-500">
                Period
              </label>
              <select
                value={period}
                onChange={(e) => setPeriod(e.target.value)}
                className="rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
              >
                {quarters.map((q) => (
                  <option key={q} value={q}>
                    {q}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div
            className={`relative flex cursor-pointer flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed py-14 transition-colors ${
              isDragging
                ? "border-primary-400 bg-primary-50"
                : "border-zinc-300 bg-zinc-50 hover:border-zinc-400 hover:bg-zinc-100"
            }`}
            onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") inputRef.current?.click(); }}
            aria-label="Upload your records"
          >
            <input
              ref={inputRef}
              type="file"
              className="sr-only"
              accept=".xlsx,.xls,.csv,.pdf,.png,.jpg,.jpeg"
              onChange={(e) => handleFiles(e.target.files)}
            />
            {uploading ? (
              <>
                <Spinner className="h-7 w-7 text-primary-500" />
                <p className="text-sm font-medium text-zinc-600">Uploading…</p>
              </>
            ) : (
              <>
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-zinc-400" aria-hidden>
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="17 8 12 3 7 8" />
                  <line x1="12" y1="3" x2="12" y2="15" />
                </svg>
                <p className="text-sm font-medium text-zinc-700">
                  Drop your records here, or <span className="text-primary-600">click to browse</span>
                </p>
                <p className="text-xs text-zinc-400">
                  .xlsx · .xls · .csv · .pdf · .png · .jpg — max 25 MB
                </p>
              </>
            )}
          </div>
        </div>
      )}

      {/* Side-by-side panels */}
      {check && (
        <>
          <div className="flex items-center justify-between">
            <div className="text-sm text-zinc-500">
              Period: <span className="font-medium text-zinc-800">{check.period_label}</span>
              {" · "}
              <span className="text-zinc-400">{check.uploaded_filename}</span>
            </div>
            <button
              type="button"
              onClick={() => {
                setCheck(null);
                setUploadedBlobUrl(null);
                setUploadedHtml(null);
                setSoHtml(null);
                setSoBlobUrl(null);
                setFlagMode(false);
                setFlagNote("");
              }}
              className="text-xs text-zinc-400 hover:text-zinc-600 focus:outline-none"
            >
              Upload a different file
            </button>
          </div>

          {/* SO workbook error — loud warning */}
          {soError && (
            <div className="rounded-xl border-2 border-amber-400 bg-amber-50 p-4">
              <p className="text-sm font-semibold text-amber-800">
                ⚠ Solar Operator workbook unavailable
              </p>
              <p className="mt-1 text-sm text-amber-700">{soError}</p>
              <p className="mt-1 text-xs text-amber-600">
                This usually means no bill data has been captured for this client yet.
                Log into the utility portal with the extension to pull their data, then try again.
              </p>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {/* Left: your records */}
            <div className="space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
                Your records
              </h2>
              <FilePanel
                check={check}
                label="your records"
                blobUrl={uploadedBlobUrl}
                sheetHtml={uploadedHtml}
                error={uploadedError}
                loading={uploadedLoading}
              />
            </div>

            {/* Right: SO workbook */}
            <div className="space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
                Solar Operator
              </h2>
              <FilePanel
                check={null}
                label="Solar Operator workbook"
                blobUrl={soBlobUrl}
                sheetHtml={soHtml}
                error={null}
                loading={soLoading}
              />
            </div>
          </div>

          {/* Resolve */}
          {check.status === "pending" && (
            <div className="rounded-2xl border border-zinc-200 bg-white p-5 shadow-sm">
              {!flagMode ? (
                <div className="flex flex-wrap gap-3">
                  <Button
                    variant="primary"
                    onClick={() => handleResolve("confirmed")}
                    disabled={resolving}
                  >
                    {resolving ? <Spinner /> : null}
                    Looks right ✓
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => setFlagMode(true)}
                    disabled={resolving}
                  >
                    Flag a difference
                  </Button>
                </div>
              ) : (
                <div className="space-y-3">
                  <label className="block text-sm font-medium text-zinc-700">
                    Describe the difference
                  </label>
                  <textarea
                    value={flagNote}
                    onChange={(e) => setFlagNote(e.target.value)}
                    rows={3}
                    placeholder="e.g. Q3 MWh for Starlake is 18.4 in my records but 17.9 in SO"
                    className="w-full rounded-xl border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary-500/40"
                  />
                  <div className="flex gap-2">
                    <Button
                      variant="danger"
                      onClick={() => handleResolve("flagged")}
                      disabled={resolving || !flagNote.trim()}
                    >
                      {resolving ? <Spinner /> : null}
                      Submit flag
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => { setFlagMode(false); setFlagNote(""); }}
                      disabled={resolving}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}

          {check.status !== "pending" && (
            <div
              className={`rounded-xl border px-4 py-3 text-sm font-medium ${
                check.status === "confirmed"
                  ? "border-primary-200 bg-primary-50 text-primary-700"
                  : "border-amber-200 bg-amber-50 text-amber-700"
              }`}
            >
              {check.status === "confirmed"
                ? "✓ Confirmed — numbers match"
                : `⚑ Flagged${check.operator_note ? ": " + check.operator_note : ""}`}
            </div>
          )}
        </>
      )}
    </div>
  );
}
