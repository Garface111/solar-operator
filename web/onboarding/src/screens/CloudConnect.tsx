import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { SO_OPERATOR_PASSWORD_KEY } from "./Info";
import {
  getToken,
  completeOnboarding,
  fetchProviders,
  saveCloudCredential,
  cloudCaptureUiEnabled,
  ONBOARDING_COMPLETED_KEY,
  type Provider,
} from "../lib/onboarding";

// Curated quick-picks — the utilities that cover the vast majority of VT/NH
// NEPOOL clients. `host` is the SmartHub login subdomain the harvester logs into
// (null for GMP, which uses its own API). The full live catalog is searchable
// below for anything else.
interface QuickPick {
  code: string;
  name: string;
  host: string | null;
}
const QUICK_PICKS: QuickPick[] = [
  { code: "gmp", name: "Green Mountain Power", host: null },
  { code: "vec", name: "Vermont Electric Co-op", host: "vermontelectric.smarthub.coop" },
  { code: "wec", name: "Washington Electric Co-op", host: "washingtonelectric.smarthub.coop" },
  { code: "stowe", name: "Stowe Electric", host: "stoweelectric.smarthub.coop" },
  { code: "nhec", name: "NH Electric Cooperative", host: "nhec.smarthub.coop" },
  { code: "ludlow", name: "Village of Ludlow Electric", host: "ludlow.smarthub.coop" },
];

interface Selected {
  code: string;
  label: string;
  host: string | null;
}
interface SavedLogin {
  code: string;
  label: string;
  username: string;
}

export default function CloudConnect() {
  const navigate = useNavigate();
  const toast = useToast();

  // 1) Complete onboarding EARLY to mint the dashboard session we store creds with.
  const [session, setSession] = useState<string | null>(null);
  const [completing, setCompleting] = useState(true);
  const [completeError, setCompleteError] = useState<string | null>(null);
  const completeRef = useRef(false);

  // 2) Utility catalog (for search) + the operator's chosen utility + form.
  const [providers, setProviders] = useState<Provider[]>([]);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Selected | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saved, setSaved] = useState<SavedLogin[]>([]);

  // Guard: this screen must only be reached with the flag on.
  useEffect(() => {
    if (!cloudCaptureUiEnabled()) navigate("/extension", { replace: true });
  }, [navigate]);

  const complete = async () => {
    if (completeRef.current) return;
    completeRef.current = true;
    const token = getToken();
    if (!token) {
      setCompleteError("We couldn't find your onboarding session. Please restart from the welcome screen.");
      setCompleting(false);
      return;
    }
    setCompleting(true);
    setCompleteError(null);
    try {
      const stashedPassword = sessionStorage.getItem(SO_OPERATOR_PASSWORD_KEY) || undefined;
      const res = await completeOnboarding(token, stashedPassword ? { password: stashedPassword } : undefined);
      if (res.session_token) {
        setSession(res.session_token);
        try {
          localStorage.setItem("so_session", res.session_token);
          sessionStorage.setItem(ONBOARDING_COMPLETED_KEY, "1"); // tell /done not to re-complete
          sessionStorage.removeItem(SO_OPERATOR_PASSWORD_KEY);
        } catch {
          /* private mode — session still held in memory for this screen */
        }
      } else {
        setCompleteError("Your account is set up, but we couldn't start a secure session to store logins. You can add them from your account settings after signing in.");
      }
    } catch (e) {
      setCompleteError(
        e instanceof Error ? e.message : "Couldn't finish setting up your account — try again in a moment.",
      );
    } finally {
      setCompleting(false);
      completeRef.current = false; // allow a retry if it failed
    }
  };

  useEffect(() => {
    void complete();
    fetchProviders().then((p) => setProviders(p)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Search results (live utilities only), excluding the quick-picks already shown.
  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    const quick = new Set(QUICK_PICKS.map((x) => x.code));
    return providers
      .filter((p) => p.scrape_status === "live" && !quick.has(p.code))
      .filter((p) => p.label.toLowerCase().includes(q) || p.state.toLowerCase().includes(q))
      .slice(0, 8);
  }, [providers, query]);

  const pickQuick = (q: QuickPick) => {
    setSelected({ code: q.code, label: q.name, host: q.host });
    setFormError(null);
  };
  const pickCatalog = (p: Provider) => {
    setSelected({ code: p.code, label: p.label, host: p.smarthub_host || null });
    setQuery("");
    setFormError(null);
  };

  const save = async () => {
    setFormError(null);
    if (!session) {
      setFormError("Still finishing account setup — one moment.");
      return;
    }
    if (!selected || !username.trim() || !password) {
      setFormError("Pick a utility and enter the username + password.");
      return;
    }
    setSaving(true);
    try {
      const r = await saveCloudCredential(session, {
        provider: selected.code,
        username: username.trim(),
        password,
        login_host: selected.host,
      });
      if (r.ok) {
        setSaved((s) => [
          ...s.filter((x) => !(x.code === selected.code && x.username === username.trim())),
          { code: selected.code, label: selected.label, username: username.trim() },
        ]);
        toast.success(`${selected.label} connected — we'll start pulling its bills.`);
        setUsername("");
        setPassword("");
        setSelected(null);
      } else {
        setFormError(r.error || "Couldn't save that login — check the details and try again.");
      }
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "Couldn't save that login.");
    }
    setSaving(false);
  };

  const goDone = () => navigate("/done");

  return (
    <ScreenLayout current={3}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Connect your utility — we&apos;ll do the rest.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          Add the login you use for your utility portal. Our servers sign in and pull the
          bills for you 24/7 — encrypted at rest, never shown again, remove anytime.
        </p>

        {/* Account setup / session state */}
        {completing && (
          <div className="mt-6 flex items-center gap-3 rounded-xl border border-primary-200 bg-primary-50 px-4 py-3">
            <Spinner />
            <p className="text-sm text-zinc-700">Finishing your account setup…</p>
          </div>
        )}
        {completeError && (
          <div className="mt-6 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3">
            <p className="text-sm text-amber-900">{completeError}</p>
            <button
              type="button"
              onClick={() => void complete()}
              className="mt-2 text-sm font-semibold text-amber-900 underline underline-offset-2 hover:text-amber-800"
            >
              Try again
            </button>
          </div>
        )}

        {/* Saved logins */}
        {saved.length > 0 && (
          <ul className="mt-6 space-y-2">
            {saved.map((s) => (
              <li
                key={`${s.code}:${s.username}`}
                className="flex items-center gap-3 rounded-xl border border-primary-200 bg-primary-50 px-4 py-2.5"
              >
                <span aria-hidden className="text-primary-600">✓</span>
                <span className="min-w-0 flex-1 text-sm">
                  <span className="font-semibold text-zinc-900">{s.label}</span>
                  <span className="ml-2 text-zinc-500">{s.username}</span>
                </span>
                <span className="shrink-0 text-xs font-medium text-primary-700">
                  Connected — refreshing
                </span>
              </li>
            ))}
          </ul>
        )}

        {/* Picker + credential form */}
        <div className={`mt-6 ${completing ? "pointer-events-none opacity-50" : ""}`}>
          {!selected ? (
            <>
              <p className="text-sm font-semibold text-zinc-900">
                {saved.length > 0 ? "Add another utility" : "Which utility does this client use?"}
              </p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">
                {QUICK_PICKS.map((q) => (
                  <button
                    key={q.code}
                    type="button"
                    onClick={() => pickQuick(q)}
                    className="flex items-center gap-3 rounded-xl border border-zinc-200 bg-white px-4 py-3 text-left transition-colors hover:bg-zinc-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  >
                    <span
                      aria-hidden
                      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary-100 text-[11px] font-semibold text-primary-700"
                    >
                      {q.code.slice(0, 3).toUpperCase()}
                    </span>
                    <span className="min-w-0 text-sm font-medium text-zinc-900">{q.name}</span>
                  </button>
                ))}
              </div>

              <div className="mt-4">
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search another utility…"
                  className="w-full rounded-xl border border-zinc-300 px-3 py-2 text-sm"
                />
                {results.length > 0 && (
                  <ul className="mt-1 overflow-hidden rounded-xl border border-zinc-200">
                    {results.map((p) => (
                      <li key={p.code}>
                        <button
                          type="button"
                          onClick={() => pickCatalog(p)}
                          className="flex w-full items-center justify-between gap-2 border-b border-zinc-100 bg-white px-3 py-2 text-left text-sm last:border-b-0 hover:bg-zinc-50"
                        >
                          <span className="font-medium text-zinc-900">{p.label}</span>
                          <span className="text-xs text-zinc-400">{p.state}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          ) : (
            <div className="rounded-xl border border-zinc-200 bg-zinc-50 p-4">
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-zinc-900">{selected.label}</p>
                <button
                  type="button"
                  onClick={() => { setSelected(null); setFormError(null); }}
                  className="text-xs font-medium text-zinc-500 hover:text-zinc-700"
                >
                  Change
                </button>
              </div>
              <input
                type="text"
                autoComplete="off"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={`Your ${selected.label} username / email`}
                className="mt-3 w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm"
              />
              <input
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Portal password"
                className="mt-2 w-full rounded-lg border border-zinc-300 px-3 py-2 text-sm"
              />
              <p className="mt-2 text-xs text-zinc-500">
                Stored encrypted on our servers so we can refresh your bills automatically.
                Remove it anytime from your account settings.
              </p>
              {formError && <p className="mt-2 text-xs text-red-600">{formError}</p>}
              <div className="mt-3">
                <Button variant="primary" onClick={() => void save()} disabled={saving || !session}>
                  {saving ? "Connecting…" : "Connect this login"}
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Footer: continue */}
        <div className="mt-8 flex items-center justify-between gap-4">
          <span className="text-xs text-zinc-500">
            {saved.length > 0
              ? "You can add more or manage logins later from your account."
              : "You can also add your login later from your account settings."}
          </span>
          <Button variant={saved.length > 0 ? "primary" : "secondary"} onClick={goDone} disabled={completing}>
            {saved.length > 0 ? "Finish →" : "Skip for now →"}
          </Button>
        </div>
      </Card>
    </ScreenLayout>
  );
}
