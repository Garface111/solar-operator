import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Toggle } from "../ui/Toggle";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import {
  getToken,
  submitClients,
  completeOnboarding,
  type ClientPayload,
} from "../lib/onboarding";

interface ArrayDraft {
  name: string;
  nepool_gis_id: string;
}

interface ClientDraft {
  id: number;
  name: string;
  contact_email: string;
  gmp_autopopulate: boolean;
  // GMP accepts either an email or a username at login; one field captures
  // whichever the client uses. Split into gmp_email / gmp_username at submit.
  gmp_login: string;
  arrays: ArrayDraft[];
}

let nextId = 1;
function blankClient(): ClientDraft {
  return {
    id: nextId++,
    name: "",
    contact_email: "",
    gmp_autopopulate: true,
    gmp_login: "",
    arrays: [],
  };
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function Clients() {
  const navigate = useNavigate();
  const toast = useToast();
  const [clients, setClients] = useState<ClientDraft[]>(() => [blankClient()]);
  const [submitting, setSubmitting] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);

  // Detect a lost onboarding session on mount so we can disable "Finish setup"
  // and surface a real restart link — instead of letting the click silently
  // no-op the operator into a dead end.
  useEffect(() => {
    if (!getToken()) {
      setSessionError(
        "We couldn't find your onboarding session. Please restart from the welcome screen.",
      );
    }
  }, []);

  function update(id: number, patch: Partial<ClientDraft>) {
    setClients((cs) => cs.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }

  function addClient() {
    setClients((cs) => [...cs, blankClient()]);
  }

  function removeClient(id: number) {
    setClients((cs) => (cs.length === 1 ? cs : cs.filter((c) => c.id !== id)));
  }

  function addArray(id: number) {
    setClients((cs) =>
      cs.map((c) =>
        c.id === id
          ? { ...c, arrays: [...c.arrays, { name: "", nepool_gis_id: "" }] }
          : c,
      ),
    );
  }

  function updateArray(id: number, idx: number, patch: Partial<ArrayDraft>) {
    setClients((cs) =>
      cs.map((c) =>
        c.id === id
          ? {
              ...c,
              arrays: c.arrays.map((a, i) => (i === idx ? { ...a, ...patch } : a)),
            }
          : c,
      ),
    );
  }

  function removeArray(id: number, idx: number) {
    setClients((cs) =>
      cs.map((c) =>
        c.id === id
          ? { ...c, arrays: c.arrays.filter((_, i) => i !== idx) }
          : c,
      ),
    );
  }

  // The Starlake case: when 2+ clients use autopop, sub-meter arrays (multiple
  // GMP accounts that should roll up into one array) will be imported as
  // separate arrays and need a manual merge in the dashboard later.
  const autopopCount = clients.filter((c) => c.gmp_autopopulate).length;
  const showSubMeterWarning = autopopCount >= 2;

  const valid = useMemo(
    () =>
      clients.every((c) => {
        if (c.name.trim().length < 1) return false;
        if (c.contact_email.trim() && !EMAIL_RE.test(c.contact_email.trim()))
          return false;
        if (c.gmp_autopopulate) {
          // Accept email OR username — just require a non-empty value.
          return c.gmp_login.trim().length >= 1;
        }
        return true;
      }),
    [clients],
  );

  async function handleFinish() {
    if (!valid || submitting) return;
    const token = getToken();
    if (!token) {
      setSessionError(
        "We couldn't find your onboarding session. Please restart from the welcome screen.",
      );
      return;
    }
    setSubmitting(true);
    setSessionError(null);

    const payload: ClientPayload[] = clients.map((c) => {
      const login = c.gmp_login.trim();
      const looksLikeEmail = EMAIL_RE.test(login);
      return {
        name: c.name.trim(),
        contact_email: c.contact_email.trim() || undefined,
        gmp_autopopulate: c.gmp_autopopulate,
        // Route the single login field to the right column: an email-shaped
        // value matches on gmp_email, anything else matches on gmp_username.
        gmp_email:
          c.gmp_autopopulate && login && looksLikeEmail ? login : undefined,
        gmp_username:
          c.gmp_autopopulate && login && !looksLikeEmail ? login : undefined,
        arrays: c.gmp_autopopulate
          ? []
          : c.arrays
              .filter((a) => a.name.trim())
              .map((a) => ({
                name: a.name.trim(),
                nepool_gis_id: a.nepool_gis_id.trim() || undefined,
              })),
      };
    });

    try {
      await submitClients(token, payload);
      const { session_token } = await completeOnboarding(token);
      // Log the operator straight into the dashboard. The onboarding SPA and the
      // dashboard SPA share an origin (solaroperator.org), so this `so_session`
      // is the same key the dashboard's AuthGate reads — they land signed in.
      if (session_token) {
        localStorage.setItem("so_session", session_token);
      }
      navigate("/done");
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't save your clients. Check your connection and try again.",
      );
      setSubmitting(false);
    }
  }

  return (
    <ScreenLayout current={3}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Add your reporting clients.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          A client is whoever receives a quarterly report. Turn on auto-populate
          to pull their arrays straight from GMP, or add arrays by hand.
        </p>

        {showSubMeterWarning && (
          <div className="mt-6 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
            <p className="font-semibold">⚠ Sub-metered arrays need a manual merge</p>
            <p className="mt-1 text-xs leading-relaxed text-amber-800">
              Auto-populate creates one array per GMP account. If a client&apos;s
              array is actually fed by several sub-meters (e.g. the Starlake
              case — 3 GMP accounts rolling up into 1 array), those will come in
              as separate arrays. You&apos;ll need to merge them by hand in the
              dashboard after onboarding. We can&apos;t detect this automatically.
            </p>
          </div>
        )}

        <div className="mt-8 space-y-6">
          {clients.map((c, idx) => (
            <div
              key={c.id}
              className="rounded-xl border border-zinc-200 p-5"
            >
              <div className="mb-4 flex items-center justify-between">
                <span className="text-sm font-semibold text-zinc-700">
                  Client {idx + 1}
                </span>
                {clients.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeClient(c.id)}
                    aria-label={`Remove client ${idx + 1}`}
                    className="rounded text-xs font-medium text-zinc-400 transition-colors duration-150 ease-in-out hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                  >
                    Remove
                  </button>
                )}
              </div>

              <div className="space-y-4">
                <Input
                  id={`name-${c.id}`}
                  label="Client name"
                  placeholder="Maple Ridge HOA"
                  value={c.name}
                  onChange={(e) => update(c.id, { name: e.target.value })}
                />
                <Input
                  id={`email-${c.id}`}
                  label="Contact email"
                  type="email"
                  placeholder="reports@mapleridge.org"
                  value={c.contact_email}
                  onChange={(e) => update(c.id, { contact_email: e.target.value })}
                />

                <div className="rounded-xl bg-zinc-50 px-4 py-3">
                  <Toggle
                    id={`autopop-${c.id}`}
                    checked={c.gmp_autopopulate}
                    onChange={(v) => update(c.id, { gmp_autopopulate: v })}
                    label="Auto-populate arrays from GMP"
                  />

                  {c.gmp_autopopulate ? (
                    <div className="mt-4">
                      <Input
                        id={`gmp-${c.id}`}
                        label="GMP login (email or username)"
                        placeholder="client@gmail.com or jdoe"
                        value={c.gmp_login}
                        onChange={(e) => update(c.id, { gmp_login: e.target.value })}
                      />
                      <p className="mt-1.5 text-xs text-zinc-500">
                        Use whichever you log into GMP with.
                      </p>
                    </div>
                  ) : (
                    <div className="mt-4">
                      <p className="mb-2 text-xs font-medium text-zinc-600">
                        Add arrays manually
                      </p>
                      <div className="space-y-3">
                        {c.arrays.map((a, ai) => (
                          <div
                            key={ai}
                            className="flex flex-col gap-2 sm:flex-row sm:items-end"
                          >
                            <div className="flex-1">
                              <Input
                                id={`arr-name-${c.id}-${ai}`}
                                label="Array name"
                                placeholder="South Field"
                                value={a.name}
                                onChange={(e) =>
                                  updateArray(c.id, ai, { name: e.target.value })
                                }
                              />
                            </div>
                            <div className="flex-1">
                              <Input
                                id={`arr-gis-${c.id}-${ai}`}
                                label="NEPOOL-GIS ID (optional)"
                                placeholder="NON12345"
                                value={a.nepool_gis_id}
                                onChange={(e) =>
                                  updateArray(c.id, ai, {
                                    nepool_gis_id: e.target.value,
                                  })
                                }
                              />
                            </div>
                            <button
                              type="button"
                              onClick={() => removeArray(c.id, ai)}
                              aria-label={`Remove array ${ai + 1}`}
                              className="self-end rounded px-2 py-2 text-zinc-400 transition-colors duration-150 ease-in-out hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2 sm:mb-1"
                            >
                              ✕
                            </button>
                          </div>
                        ))}
                      </div>
                      <button
                        type="button"
                        onClick={() => addArray(c.id)}
                        className="mt-3 rounded text-sm font-medium text-primary-600 transition-colors duration-150 ease-in-out hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                      >
                        + Add array
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>

        <button
          type="button"
          onClick={addClient}
          className="mt-6 rounded text-sm font-medium text-primary-600 transition-colors duration-150 ease-in-out hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
        >
          + Add another client
        </button>

        {sessionError && (
          <div className="mt-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
            <p className="text-sm text-red-700">{sessionError}</p>
            <Link
              to="/"
              className="mt-2 inline-flex items-center gap-1 text-sm font-medium text-red-700 underline underline-offset-2 hover:text-red-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40 focus-visible:ring-offset-2"
            >
              Restart setup →
            </Link>
          </div>
        )}

        <div className="mt-8 flex flex-col items-end gap-1.5">
          <Button
            onClick={handleFinish}
            disabled={!valid || submitting || !!sessionError}
          >
            {submitting ? (
              <>
                <Spinner />
                Finishing…
              </>
            ) : (
              "Finish setup →"
            )}
          </Button>
          {sessionError && (
            <p className="text-xs text-zinc-400">
              Session expired — restart above to continue.
            </p>
          )}
        </div>
      </Card>
    </ScreenLayout>
  );
}
