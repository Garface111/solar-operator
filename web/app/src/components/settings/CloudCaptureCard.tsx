import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type KeyboardEvent,
} from "react";
import { Spinner } from "../../ui/Spinner";
import { Button } from "../../ui/Button";
import { timeAgo } from "./utils";
import {
  getCloudCaptureStatus,
  getProviders,
  setCloudCredential,
  deleteCloudCredential,
  refreshCloudCapture,
  requestUtilityAddition,
  type CloudCredential,
  type ProviderEntry,
} from "../../lib/api";

/**
 * Auto-refresh utility vault (NEPOOL Account).
 *
 * Design mirrors Array Operator's Account → Auto-refresh utility portals:
 * searchable catalog, grid of utility cards, multiple logins per utility.
 * Backends stay NEPOOL-only (`/v1/cloud-capture/*` + `/v1/providers`) —
 * no Array Operator fleet/offtaker wiring.
 */

const DEFAULT_CODES = ["gmp", "eversource", "cmp"] as const;

type Draft = { username: string; password: string; consent: boolean };

function emptyDraft(): Draft {
  return { username: "", password: "", consent: false };
}

function statusLine(c: CloudCredential): { text: string; cls: string } {
  const fails = c.harvest_fails || 0;
  const st = (c.last_harvest_status || "").toLowerCase();
  if (fails >= 3)
    return { text: "Paused — re-enter the password to retry", cls: "text-amber-700" };
  if (st === "login_failed")
    return { text: "Couldn't sign in — check the password", cls: "text-amber-700" };
  if (st === "scrape_failed")
    return {
      text: "Signed in, data pull hit a snag — retrying",
      cls: "text-amber-700",
    };
  if (c.last_harvest_at && c.last_harvest_ok === false && !st)
    return { text: "Last pull had a snag — retrying", cls: "text-amber-700" };
  if (c.last_harvest_at && c.last_harvest_ok !== false)
    return {
      text: `Connected · refreshed ${timeAgo(new Date(c.last_harvest_at))}`,
      cls: "text-emerald-700",
    };
  if (c.last_harvest_at)
    return {
      text: `Last attempt ${timeAgo(new Date(c.last_harvest_at))}`,
      cls: "text-zinc-500",
    };
  return { text: "Saved — first refresh starting…", cls: "text-zinc-500" };
}

export function CloudCaptureCard({ compact }: { compact?: boolean }) {
  const [loading, setLoading] = useState(true);
  const [creds, setCreds] = useState<CloudCredential[]>([]);
  const [providers, setProviders] = useState<ProviderEntry[]>([]);
  const [addedCodes, setAddedCodes] = useState<string[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [pickIdx, setPickIdx] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [requesting, setRequesting] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await getCloudCaptureStatus();
      setCreds(s.credentials ?? []);
    } catch {
      /* list empty; form still usable */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      await load();
      try {
        const p = await getProviders();
        if (!cancelled) setProviders(p.filter((x) => x.scrape_status === "live"));
      } catch {
        /* catalog optional */
      }
      if (!cancelled) setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [load]);

  const byCode = useMemo(() => {
    const map = new Map<string, ProviderEntry>();
    for (const p of providers) map.set(p.code, p);
    return map;
  }, [providers]);

  const labelFor = useCallback(
    (code: string) => {
      const p = byCode.get(code);
      if (p) return p.state ? `${p.label} · ${p.state}` : p.label;
      if (code === "gmp") return "Green Mountain Power";
      if (code === "eversource") return "Eversource Energy";
      if (code === "cmp") return "Central Maine Power";
      return code.toUpperCase();
    },
    [byCode],
  );

  const loginsByCode = useMemo(() => {
    const m = new Map<string, CloudCredential[]>();
    for (const c of creds) {
      const list = m.get(c.provider) || [];
      list.push(c);
      m.set(c.provider, list);
    }
    return m;
  }, [creds]);

  const shownCodes = useMemo(() => {
    const codes: string[] = [];
    const push = (c: string) => {
      if (c && !codes.includes(c)) codes.push(c);
    };
    for (const d of DEFAULT_CODES) push(d);
    for (const code of [...loginsByCode.keys()].sort()) push(code);
    for (const c of addedCodes) push(c);
    return codes;
  }, [loginsByCode, addedCodes]);

  const refreshingCount = useMemo(
    () => creds.filter((c) => c.enabled !== false).length,
    [creds],
  );

  const draftFor = (code: string): Draft => drafts[code] || emptyDraft();
  const setDraft = (code: string, patch: Partial<Draft>) => {
    setDrafts((prev) => ({
      ...prev,
      [code]: { ...emptyDraft(), ...prev[code], ...patch },
    }));
  };

  const saveLogin = async (code: string) => {
    const d = draftFor(code);
    setMsg(null);
    if (!d.username.trim() || !d.password) {
      setMsg({ kind: "err", text: "Enter username and password for this utility." });
      return;
    }
    if (!d.consent) {
      setMsg({
        kind: "err",
        text: "Tick the box to store this password encrypted on our servers.",
      });
      return;
    }
    const host = byCode.get(code)?.smarthub_host || null;
    setBusyKey(`${code}:new`);
    try {
      await setCloudCredential({
        provider: code,
        username: d.username.trim(),
        password: d.password,
        login_host: host,
        enable: true,
        consent: true,
      });
      setDrafts((prev) => ({ ...prev, [code]: emptyDraft() }));
      setMsg({ kind: "ok", text: `Saved ${labelFor(code)} — refresh starts shortly.` });
      await load();
    } catch (e) {
      setMsg({
        kind: "err",
        text: e instanceof Error ? e.message : "Couldn't save that login.",
      });
    }
    setBusyKey(null);
  };

  const removeLogin = async (c: CloudCredential) => {
    setBusyKey(`${c.provider}:${c.username}`);
    try {
      await deleteCloudCredential(c.provider, c.username);
      await load();
    } catch {
      /* reload either way */
      await load();
    }
    setBusyKey(null);
  };

  const doRefresh = async () => {
    setRefreshing(true);
    try {
      const r = await refreshCloudCapture();
      setMsg({
        kind: "ok",
        text:
          r.queued > 0
            ? `Queued ${r.queued} login${r.queued === 1 ? "" : "s"} for a fresh pull.`
            : "No enabled logins to refresh yet.",
      });
      await load();
    } catch (e) {
      setMsg({
        kind: "err",
        text: e instanceof Error ? e.message : "Couldn't refresh.",
      });
    }
    setRefreshing(false);
  };

  const liveProviders = useMemo(
    () =>
      providers.slice().sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: "base" })),
    [providers],
  );

  const searchHits = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) {
      // Popular first without a query
      const popular = ["gmp", "eversource", "cmp", "vec", "wec"];
      const hits: ProviderEntry[] = [];
      for (const code of popular) {
        const p = byCode.get(code);
        if (p) hits.push(p);
      }
      for (const p of liveProviders) {
        if (hits.length >= 12) break;
        if (!hits.some((h) => h.code === p.code)) hits.push(p);
      }
      return hits;
    }
    return liveProviders
      .filter((p) => {
        const blob = `${p.label} ${p.state} ${p.code} ${p.smarthub_host || ""}`.toLowerCase();
        return blob.includes(q);
      })
      .slice(0, 40);
  }, [query, liveProviders, byCode]);

  const pickUtility = (code: string) => {
    setAddedCodes((prev) => (prev.includes(code) ? prev : [...prev, code]));
    setPickerOpen(false);
    setQuery("");
    setPickIdx(0);
    // Scroll to card after paint
    requestAnimationFrame(() => {
      document
        .getElementById(`ar-util-${code}`)
        ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  };

  const requestMissing = async () => {
    const name = query.trim();
    if (!name || requesting) return;
    setRequesting(true);
    try {
      await requestUtilityAddition({
        utility_name: name,
        notes: "Requested from NEPOOL Account → Auto-refresh utility picker",
      });
      setMsg({
        kind: "ok",
        text: `Got it — we'll wire up “${name}” and email you when it's live.`,
      });
      setQuery("");
      setPickerOpen(false);
    } catch (e) {
      setMsg({
        kind: "err",
        text: e instanceof Error ? e.message : "Couldn't submit that request.",
      });
    }
    setRequesting(false);
  };

  const onPickerKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setPickIdx((i) => Math.min(i + 1, Math.max(0, searchHits.length - 1)));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setPickIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (searchHits[pickIdx]) pickUtility(searchHits[pickIdx].code);
      else if (query.trim()) void requestMissing();
    } else if (e.key === "Escape") {
      setPickerOpen(false);
    }
  };

  return (
    <section
      id="rowAutoRefresh"
      className="mb-6 overflow-hidden rounded-2xl border border-zinc-200 bg-white shadow-sm"
    >
      {/* Head — AO Auto-refresh style */}
      <div className="flex gap-3 border-b border-zinc-100 px-5 py-4 sm:px-6">
        <button
          type="button"
          onClick={() => void doRefresh()}
          disabled={refreshing || loading}
          title="Refresh now"
          aria-label="Refresh cloud status now"
          className={`grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-zinc-200 bg-zinc-50 text-lg text-zinc-700 transition hover:border-primary-300 hover:bg-primary-50 hover:text-primary-800 disabled:opacity-50 ${
            refreshing ? "animate-spin" : ""
          }`}
        >
          ↻
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
              Auto-refresh
            </h2>
            {refreshingCount > 0 && (
              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-[11px] font-semibold text-emerald-800">
                {refreshingCount} portal{refreshingCount === 1 ? "" : "s"} refreshing
              </span>
            )}
            <span className="rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-semibold text-primary-700">
              Cloud
            </span>
          </div>
          {!compact && (
            <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-zinc-500">
              Keeps utility bills fresh automatically. Logins are{" "}
              <b className="font-semibold text-zinc-700">encrypted on our servers</b>
              — no open tab or extension needed. Add multiple logins per utility.
            </p>
          )}
        </div>
      </div>

      <div className="space-y-4 px-5 py-4 sm:px-6">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-zinc-500">
            <Spinner /> Loading portals…
          </div>
        ) : (
          <>
            <div>
              <div className="mb-1 flex items-center gap-2">
                <span className="inline-block h-0.5 w-3.5 rounded bg-primary-500" />
                <span className="text-[11px] font-extrabold uppercase tracking-[0.09em] text-zinc-900">
                  Utility portals
                </span>
              </div>
              <p className="mb-3 text-[12px] text-zinc-500">
                Utility bills, refreshed daily — powers automatic client NEPOOL
                reports. Add a login for each utility portal account.
              </p>

              {/* Grid of utility cards */}
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {shownCodes.map((code) => {
                  const logins = loginsByCode.get(code) || [];
                  const draft = draftFor(code);
                  const saving = busyKey === `${code}:new`;
                  return (
                    <div
                      key={code}
                      id={`ar-util-${code}`}
                      className="flex flex-col gap-2 rounded-xl border border-zinc-200 bg-zinc-50/60 p-3"
                    >
                      <div className="text-[13.5px] font-bold text-zinc-900">
                        {labelFor(code)}
                      </div>

                      {logins.map((c) => {
                        const st = statusLine(c);
                        const removing = busyKey === `${c.provider}:${c.username}`;
                        return (
                          <div
                            key={`${c.provider}:${c.username}`}
                            className="rounded-lg border border-zinc-200 bg-white px-3 py-2.5"
                          >
                            <div className="flex items-start justify-between gap-2">
                              <div className="min-w-0">
                                <div className="truncate text-sm font-medium text-zinc-900">
                                  {c.username}
                                </div>
                                <div className={`text-xs ${st.cls}`}>{st.text}</div>
                              </div>
                              <button
                                type="button"
                                onClick={() => void removeLogin(c)}
                                disabled={!!busyKey}
                                className="shrink-0 text-xs font-semibold text-zinc-500 hover:text-red-600 disabled:opacity-50"
                              >
                                {removing ? "…" : "Remove"}
                              </button>
                            </div>
                          </div>
                        );
                      })}

                      {/* Add login row (always — supports multi-login per utility) */}
                      <div className="flex flex-col gap-1.5 rounded-lg border border-dashed border-zinc-200 bg-white px-3 py-2.5">
                        <input
                          type="text"
                          autoComplete="off"
                          placeholder={
                            logins.length
                              ? "another username / email"
                              : "portal username / email"
                          }
                          value={draft.username}
                          onChange={(e) =>
                            setDraft(code, { username: e.target.value })
                          }
                          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-primary-500 focus:outline-none"
                        />
                        <input
                          type="password"
                          autoComplete="new-password"
                          placeholder="portal password"
                          value={draft.password}
                          onChange={(e) =>
                            setDraft(code, { password: e.target.value })
                          }
                          className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm focus:border-primary-500 focus:outline-none"
                        />
                        <label className="flex items-start gap-2 text-[11px] leading-snug text-zinc-600">
                          <input
                            type="checkbox"
                            checked={draft.consent}
                            onChange={(e) =>
                              setDraft(code, { consent: e.target.checked })
                            }
                            className="mt-0.5"
                          />
                          Store encrypted on our servers so bills refresh 24/7. Remove
                          anytime.
                        </label>
                        <div>
                          <Button
                            variant="primary"
                            onClick={() => void saveLogin(code)}
                            disabled={!!busyKey}
                            className="!px-3 !py-1.5 !text-xs"
                          >
                            {saving
                              ? "Saving…"
                              : logins.length
                                ? "Add login"
                                : "Save"}
                          </Button>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* + Add a utility — searchable picker */}
              <div className="mt-3">
                {!pickerOpen ? (
                  <button
                    type="button"
                    onClick={() => {
                      setPickerOpen(true);
                      setPickIdx(0);
                    }}
                    className="w-full rounded-xl border border-dashed border-zinc-300 bg-white px-4 py-3 text-sm font-semibold text-zinc-600 transition hover:border-primary-400 hover:bg-primary-50/40 hover:text-primary-800"
                  >
                    + Add a utility login
                  </button>
                ) : (
                  <div className="rounded-2xl border border-zinc-200 bg-white p-3 shadow-lg shadow-zinc-200/60">
                    <input
                      type="text"
                      autoFocus
                      autoComplete="off"
                      role="combobox"
                      aria-autocomplete="list"
                      aria-expanded="true"
                      placeholder="Search your utility — GMP, a co-op, a city…"
                      value={query}
                      onChange={(e) => {
                        setQuery(e.target.value);
                        setPickIdx(0);
                      }}
                      onKeyDown={onPickerKey}
                      className="w-full rounded-xl border border-zinc-300 px-3.5 py-2.5 text-sm focus:border-primary-500 focus:outline-none"
                    />
                    <div
                      className="mt-2 max-h-72 min-h-[7.5rem] overflow-y-auto rounded-xl border border-zinc-200 bg-white p-1"
                      role="listbox"
                    >
                      {!query.trim() && (
                        <div className="px-3 py-2 text-[11.5px] text-zinc-400">
                          Popular utilities, or type a name, co-op, city, or state
                        </div>
                      )}
                      {searchHits.map((p, i) => {
                        const already = shownCodes.includes(p.code);
                        return (
                          <button
                            key={p.code}
                            type="button"
                            role="option"
                            aria-selected={i === pickIdx}
                            onMouseEnter={() => setPickIdx(i)}
                            onClick={() => pickUtility(p.code)}
                            className={`flex w-full items-center gap-2 rounded-lg px-3 py-2.5 text-left text-sm transition ${
                              i === pickIdx
                                ? "border border-primary-300 bg-primary-50"
                                : already
                                  ? "border border-primary-200 bg-primary-50/50"
                                  : "border border-transparent hover:bg-zinc-50"
                            }`}
                          >
                            <span className="min-w-0 flex-1 font-semibold text-zinc-900">
                              {p.label}
                            </span>
                            {p.state ? (
                              <span className="shrink-0 text-[10.5px] font-bold uppercase tracking-wide text-zinc-400">
                                {p.state}
                              </span>
                            ) : null}
                            {already ? (
                              <span className="shrink-0 text-[10px] font-bold uppercase text-primary-700">
                                on card
                              </span>
                            ) : null}
                          </button>
                        );
                      })}
                      {query.trim() && searchHits.length === 0 && (
                        <div className="px-3 py-2 text-[11.5px] text-zinc-400">
                          No match for “{query.trim()}”
                        </div>
                      )}
                      {query.trim() && (
                        <button
                          type="button"
                          onClick={() => void requestMissing()}
                          disabled={requesting}
                          className="mt-1 flex w-full items-start gap-2 rounded-lg border border-dashed border-violet-300 bg-violet-50/50 px-3 py-2.5 text-left transition hover:bg-violet-50 disabled:opacity-50"
                        >
                          <span className="grid h-6 w-6 shrink-0 place-items-center rounded-lg bg-violet-500 text-sm font-bold text-white">
                            ＋
                          </span>
                          <span className="min-w-0">
                            <span className="block text-sm font-semibold text-zinc-900">
                              {requesting
                                ? "Sending…"
                                : `Request “${query.trim()}”`}
                            </span>
                            <span className="block text-[11.5px] text-zinc-500">
                              Not in our list yet — we&apos;ll wire it up for you
                            </span>
                          </span>
                        </button>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        setPickerOpen(false);
                        setQuery("");
                      }}
                      className="mt-2 text-xs font-semibold text-zinc-500 hover:text-zinc-800"
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </div>
            </div>

            {msg && (
              <div
                className={`rounded-lg px-3 py-2 text-xs font-medium ${
                  msg.kind === "ok"
                    ? "bg-emerald-50 text-emerald-800"
                    : "bg-red-50 text-red-700"
                }`}
              >
                {msg.text}
              </div>
            )}

            <p className="text-[11px] leading-relaxed text-zinc-400">
              Automated sign-ins may trigger a security notice from the utility — that
              is expected for cloud refresh. Passwords are never shown again after
              save.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
