// Capture timeline page — shows every extension capture event chronologically
// with stage timestamps, decision text, and truncated payload excerpt.
// Route: /accounts/dev/captures
// Gated: only renders when SO_DEV_ENABLED is on (server returns 403 otherwise).

import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listCaptures, type CaptureGroup, type CaptureEventRow } from "../../lib/api";

const POLL_INTERVAL_MS = 5000;

const STAGE_COLOR: Record<string, string> = {
  ingest_received: "text-blue-400",
  client_created: "text-green-400",
  client_matched: "text-emerald-400",
  client_merged: "text-yellow-400",
  array_created: "text-cyan-400",
  array_skipped: "text-zinc-400",
  capture_error: "text-red-400",
};

function stageColor(stage: string): string {
  return STAGE_COLOR[stage] ?? "text-zinc-300";
}

function severityClass(group: CaptureGroup): string {
  if (group.has_error) return "border-red-500/40 bg-red-950/20";
  if (group.stage_count < 2) return "border-yellow-500/40 bg-yellow-950/10";
  return "border-zinc-700/50 bg-zinc-800/30";
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtMs(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function CaptureRow({ group }: { group: CaptureGroup }) {
  const [open, setOpen] = useState(false);

  const severity = group.has_error ? "red" : group.stage_count < 2 ? "amber" : "green";
  const icon = severity === "red" ? "✗" : severity === "amber" ? "!" : "✓";
  const iconClass =
    severity === "red"
      ? "text-red-400"
      : severity === "amber"
        ? "text-amber-400"
        : "text-green-400";

  const label = group.client_hint
    ? group.client_hint.replace(/^(created|adopted placeholder|matched)\s+/i, "").replace(/^'|'$/g, "")
    : "unknown client";

  return (
    <div className={`rounded-lg border ${severityClass(group)}`}>
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs"
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`font-bold ${iconClass}`}>{icon}</span>
        <span className="font-mono text-zinc-300">{fmtTime(group.started_at)}</span>
        <span className="flex-1 truncate font-medium text-zinc-200">{label}</span>
        <span className="shrink-0 text-zinc-500">
          {group.arrays_created} array{group.arrays_created !== 1 ? "s" : ""} &middot;{" "}
          {fmtMs(group.total_ms)}
        </span>
        <span className="shrink-0 text-zinc-500">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="border-t border-zinc-700/50 px-3 pb-3 pt-2">
          <div className="mb-2 font-mono text-[10px] text-zinc-500">
            capture_id: {group.capture_id}
          </div>
          <div className="space-y-2">
            {group.events.map((ev) => (
              <EventRow key={ev.id} ev={ev} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function EventRow({ ev }: { ev: CaptureEventRow }) {
  const [copied, setCopied] = useState(false);

  const copyPayload = useCallback(() => {
    if (!ev.payload_excerpt) return;
    void navigator.clipboard.writeText(
      JSON.stringify(ev.payload_excerpt, null, 2),
    ).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [ev.payload_excerpt]);

  return (
    <div className="rounded-md bg-zinc-900/60 px-2.5 py-2 text-xs">
      <div className="flex items-baseline gap-2">
        <span className={`shrink-0 font-mono font-semibold ${stageColor(ev.stage)}`}>
          {ev.stage}
        </span>
        {ev.duration_ms != null && (
          <span className="shrink-0 font-mono text-[10px] text-zinc-600">
            +{fmtMs(ev.duration_ms)}
          </span>
        )}
        <span className="min-w-0 flex-1 truncate text-zinc-400">
          {ev.decision ?? ""}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-zinc-600">
          {new Date(ev.created_at).toLocaleTimeString()}
        </span>
      </div>

      {ev.payload_excerpt && (
        <div className="mt-1.5 rounded bg-zinc-950/80 p-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] text-zinc-600">payload_excerpt</span>
            <button
              type="button"
              onClick={copyPayload}
              className="text-[10px] text-zinc-500 hover:text-zinc-300"
            >
              {copied ? "copied!" : "copy"}
            </button>
          </div>
          <pre className="mt-1 max-h-40 overflow-auto font-mono text-[10px] leading-tight text-zinc-400">
            {JSON.stringify(ev.payload_excerpt, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

export default function CaptureTimeline() {
  const [captures, setCaptures] = useState<CaptureGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();
  const mountedRef = useRef(true);

  useEffect(() => () => { mountedRef.current = false; }, []);

  const load = useCallback(async () => {
    try {
      const data = await listCaptures(50);
      if (mountedRef.current) {
        setCaptures(data);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : "Failed to load captures");
      }
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  // Initial load
  useEffect(() => { void load(); }, [load]);

  // Auto-refresh every 5s, only when the tab is visible.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "visible") void load();
    };
    const id = setInterval(tick, POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-zinc-100">Capture Timeline</h2>
          <p className="text-xs text-zinc-500">
            Per-capture ingest events &middot; SO_DEV_ENABLED &middot; auto-refreshes every 5s
          </p>
        </div>
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="text-xs text-zinc-500 hover:text-zinc-300"
        >
          &larr; Back
        </button>
      </div>

      {loading && (
        <div className="py-8 text-center text-sm text-zinc-500">Loading&hellip;</div>
      )}

      {!loading && error && (
        <div className="rounded-lg border border-red-500/40 bg-red-950/20 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      {!loading && !error && captures.length === 0 && (
        <div className="rounded-lg border border-zinc-700/50 bg-zinc-800/30 py-12 text-center text-sm text-zinc-500">
          No captures yet. Trigger one from the extension or the DevPanel seeder.
        </div>
      )}

      {!loading && !error && captures.length > 0 && (
        <div className="space-y-2">
          {captures.map((g) => (
            <CaptureRow key={g.capture_id} group={g} />
          ))}
        </div>
      )}
    </div>
  );
}
