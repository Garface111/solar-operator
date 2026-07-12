import { useEffect, useMemo, useState } from "react";
import { Spinner } from "../../ui/Spinner";
import { Button } from "../../ui/Button";
import { timeAgo } from "./utils";
import {
  getCloudCaptureStatus,
  getProviders,
  setCloudCredential,
  deleteCloudCredential,
  type CloudCredential,
  type ProviderEntry,
} from "../../lib/api";
import { cloudCaptureUiEnabled } from "../../lib/flags";

/**
 * Cloud Capture Credential Vault (NEPOOL) — store a utility login and the
 * server-side harvester signs in and pulls the bills around the clock, no
 * extension or open tab. DARK-SHIPPED: rendered only when the runtime flag is on
 * (`so:flag:cloud-capture-ui`), so it isn't live for real operators until tested.
 * Backend is the already-product-agnostic /v1/cloud-capture/* + harvester.
 */
export function CloudCaptureCard() {
  const on = cloudCaptureUiEnabled();

  const [loading, setLoading] = useState(true);
  const [creds, setCreds] = useState<CloudCredential[]>([]);
  const [providers, setProviders] = useState<ProviderEntry[]>([]);
  const [provider, setProvider] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const s = await getCloudCaptureStatus();
      setCreds(s.credentials ?? []);
    } catch {
      /* leave the list empty; the add form still works */
    }
    setLoading(false);
  };

  useEffect(() => {
    if (!on) return;
    void load();
    // Utility catalog for the picker — only "live" (connectable) utilities.
    getProviders()
      .then((p) => setProviders(p.filter((x) => x.scrape_status === "live")))
      .catch(() => {});
  }, [on]);

  const selected = useMemo(
    () => providers.find((p) => p.code === provider),
    [providers, provider],
  );

  if (!on) return null;

  const save = async () => {
    setMsg(null);
    if (!provider || !username.trim() || !password) {
      setMsg({ kind: "err", text: "Pick a utility and enter the username + password." });
      return;
    }
    if (!consent) {
      setMsg({ kind: "err", text: "Please tick the box to store the password server-side." });
      return;
    }
    setBusy(true);
    try {
      const r = await setCloudCredential({
        provider,
        username: username.trim(),
        password,
        login_host: selected?.smarthub_host || null,
        enable: true,
        consent: true,
      });
      if (r.ok) {
        setMsg({ kind: "ok", text: "Saved — our servers will start refreshing this login." });
        setUsername("");
        setPassword("");
        setConsent(false);
        await load();
      } else {
        setMsg({ kind: "err", text: r.error || "Couldn't save that login." });
      }
    } catch (e) {
      setMsg({ kind: "err", text: e instanceof Error ? e.message : "Couldn't save that login." });
    }
    setBusy(false);
  };

  const remove = async (c: CloudCredential) => {
    setBusy(true);
    try {
      await deleteCloudCredential(c.provider, c.username);
      await load();
    } catch {
      /* ignore — the list reloads either way */
    }
    setBusy(false);
  };

  const statusLine = (c: CloudCredential): { text: string; cls: string } => {
    if ((c.harvest_fails || 0) >= 3)
      return { text: "Paused — re-enter the password to retry", cls: "text-amber-700" };
    if (c.last_harvest_at && c.last_harvest_ok === false)
      return { text: "Couldn't sign in — check the password", cls: "text-amber-700" };
    if (c.last_harvest_at)
      return { text: `Connected — refreshed ${timeAgo(new Date(c.last_harvest_at))}`, cls: "text-emerald-700" };
    return { text: "Saved — first refresh starting…", cls: "text-zinc-500" };
  };

  const labelFor = (code: string) =>
    providers.find((p) => p.code === code)?.label || code.toUpperCase();

  return (
    <section className="rounded-xl border border-zinc-200 bg-white p-5 shadow-sm">
      <div className="mb-1 flex items-center gap-2">
        <h2 className="text-base font-semibold text-zinc-900">Cloud Capture</h2>
        <span className="rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-semibold text-primary-700">
          Beta
        </span>
      </div>
      <p className="mb-4 text-sm text-zinc-600">
        Store a utility login and our servers sign in and pull the bills for you around the
        clock — no browser tab, no extension. Passwords are encrypted at rest and never shown
        again; remove any login anytime.
      </p>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-zinc-500">
          <Spinner /> Loading…
        </div>
      ) : (
        <>
          {creds.length > 0 && (
            <ul className="mb-4 divide-y divide-zinc-100 rounded-lg border border-zinc-100">
              {creds.map((c) => {
                const st = statusLine(c);
                return (
                  <li
                    key={`${c.provider}:${c.username}`}
                    className="flex items-center justify-between gap-3 px-3 py-2.5"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-zinc-900">
                        {labelFor(c.provider)}
                        <span className="ml-2 font-normal text-zinc-500">{c.username}</span>
                      </div>
                      <div className={`text-xs ${st.cls}`}>{st.text}</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => void remove(c)}
                      disabled={busy}
                      className="shrink-0 text-xs font-semibold text-zinc-500 hover:text-red-600 disabled:opacity-50"
                    >
                      Remove
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          <div className="space-y-2 rounded-lg bg-zinc-50 p-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Add a utility login
            </div>
            <select
              value={provider}
              onChange={(e) => setProvider(e.target.value)}
              className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm"
            >
              <option value="">Choose a utility…</option>
              {providers.map((p) => (
                <option key={p.code} value={p.code}>
                  {p.label}
                  {p.state ? ` · ${p.state}` : ""}
                </option>
              ))}
            </select>
            <input
              type="text"
              autoComplete="off"
              placeholder="Portal username / email"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm"
            />
            <input
              type="password"
              autoComplete="new-password"
              placeholder="Portal password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm"
            />
            <label className="flex items-start gap-2 text-xs text-zinc-600">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
                className="mt-0.5"
              />
              I authorize storing this password, encrypted, on the server so it can refresh my
              utility bills automatically. I can remove it anytime.
            </label>
            {msg && (
              <div className={`text-xs ${msg.kind === "ok" ? "text-emerald-700" : "text-red-600"}`}>
                {msg.text}
              </div>
            )}
            <Button variant="primary" onClick={() => void save()} disabled={busy}>
              {busy ? "Saving…" : "Save login"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
