import { useEffect, useRef, useState } from "react";
import { Spinner } from "../ui/Spinner";

interface Props {
  value: string | null;
  /** Commit a new value (or null to clear). May be async; spinner shows while in flight. */
  onSave: (next: string | null) => Promise<void> | void;
}

/**
 * Compact inline NEPOOL-GIS ID editor for the array list rows.
 *
 *  - Empty  → orange dot (8px) + "Add ID" ghost text. The dot carries the
 *             5-digit-ID explainer as a hover tooltip (the per-row helper
 *             paragraph that used to live below the field is gone).
 *  - Filled → the value + an emerald check glyph.
 *
 * Clicking either state morphs it into a digits-only 5-char input that
 * auto-focuses, validates against the 5-digit regex, saves on blur/Enter and
 * reverts on Escape. No modal — inline into the row only.
 *
 * The surrounding row wraps this in `[data-nepool-field]`; the guided-fill
 * "Take me to next NEPOOL ID" walk clicks the rendered <button> then focuses
 * the resulting <input>, so those two element shapes must stay.
 */
export function InlineNepoolField({ value, onSave }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!editing) setDraft(value ?? "");
  }, [value, editing]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  async function commit() {
    const next = draft.trim();
    const current = (value ?? "").trim();
    if (next !== "" && !/^\d{5}$/.test(next)) {
      setError("5 digits");
      inputRef.current?.focus();
      return;
    }
    setEditing(false);
    setError("");
    if (next === current) return;
    setSaving(true);
    try {
      await onSave(next || null);
    } catch {
      // Roll back to last known-good value on failure (caller toasts).
      setDraft(value ?? "");
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <span className="inline-flex flex-col">
        <input
          ref={inputRef}
          inputMode="numeric"
          value={draft}
          placeholder="53984"
          maxLength={5}
          aria-label="NEPOOL-GIS ID"
          onChange={(e) => {
            setDraft(e.target.value.replace(/\D/g, "").slice(0, 5));
            if (error) setError("");
          }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              (e.target as HTMLInputElement).blur();
            } else if (e.key === "Escape") {
              setDraft(value ?? "");
              setEditing(false);
              setError("");
            }
          }}
          className={[
            "w-20 rounded-md border bg-white px-2 py-0.5 text-sm tabular-nums",
            "focus:outline-none focus:ring-2 focus:ring-primary-500/40",
            error ? "border-red-400 focus:ring-red-400/40" : "border-primary-400",
          ].join(" ")}
        />
        {error && <span className="mt-0.5 text-[11px] text-red-600">{error}</span>}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      aria-label={value ? `Edit NEPOOL ID ${value}` : "Add NEPOOL ID"}
      className="group inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-sm transition-colors hover:bg-zinc-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
    >
      {saving ? (
        <Spinner className="h-3 w-3 text-zinc-400" />
      ) : value ? (
        <>
          <span className="tabular-nums text-zinc-800">{value}</span>
          <span aria-hidden className="text-emerald-600">
            ✓
          </span>
        </>
      ) : (
        <>
          <span
            aria-hidden
            data-nepool-dot
            title="5-digit ISO-NE asset ID — required to ship reports. You can add it later if you don't have it now."
            className="h-2 w-2 shrink-0 rounded-full bg-amber-500"
          />
          <span className="text-zinc-400 group-hover:text-zinc-600">Add ID</span>
        </>
      )}
    </button>
  );
}
