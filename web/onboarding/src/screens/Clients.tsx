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
  fetchStatus,
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

interface ConfirmSummary {
  clients: Array<{ name: string; contact_email: string; arrayCount: number; autopop: boolean }>;
}

export default function Clients() {
  const navigate = useNavigate();
  const toast = useToast();
  const [clients, setClients] = useState<ClientDraft[]>(() => [blankClient()]);
  const [submitting, setSubmitting] = useState(false);
  const [completing, setCompleting] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [confirmSummary, setConfirmSummary] = useState<ConfirmSummary | null>(null);
  // Path A: operator pre-entered clients before checkout — detect and skip re-entry.
  const [existingClientsCount, setExistingClientsCount] = useState(0);
  const [existingArraysCount, setExistingArraysCount] = useState(0);
  const [statusChecked, setStatusChecked] = useState(false);
  const [addingMore, setAddingMore] = useState(false);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      setSessionError(
        "We couldn't find your onboarding session. Please restart from the welcome screen.",
      );
      setStatusChecked(true);
      return;
    }
    fetchStatus(token)
      .then((s) => {
        setExistingClientsCount(s.clients_count);
        setExistingArraysCount(s.arrays_count);
      })
      .catch(() => { /* non-fatal — fall through to normal form */ })
      .finally(() => setStatusChecked(true));
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

  // When 2+ clients use autopop, sub-meter arrays (multiple GMP accounts that
  // should roll up into one array) will be imported as separate arrays and need
  // a manual merge in the dashboard later.
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

  async function handleSubmitClients() {
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
      // Show the confirmation sub-screen before completing onboarding.
      setConfirmSummary({
        clients: clients.map((c) => ({
          name: c.name.trim(),
          contact_email: c.contact_email.trim(),
          arrayCount: c.gmp_autopopulate ? 0 : c.arrays.filter((a) => a.name.trim()).length,
          autopop: c.gmp_autopopulate,
        })),
      });
      setSubmitting(false);
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't save your clients. Check your connection and try again.",
      );
      setSubmitting(false);
    }
  }

  async function handleComplete() {
    if (completing) return;
    const token = getToken();
    if (!token) return;
    setCompleting(true);
    try {
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
          : "Couldn't complete setup. Check your connection and try again.",
      );
      setCompleting(false);
    }
  }

  // Path A: clients were pre-entered before checkout — show summary, skip re-entry.
  if (statusChecked && existingClientsCount > 0 && !addingMore) {
    return (
      <ScreenLayout current={4}>
        <Card active>
          <div aria-hidden className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary-100 text-2xl text-primary-600">
            ✓
          </div>
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-zinc-900 text-center">
            Your clients are already set up.
          </h1>
          <p className="mt-2 text-center text-sm text-zinc-500">
            You entered{" "}
            <span className="font-medium text-zinc-700">{existingClientsCount} client{existingClientsCount === 1 ? "" : "s"}</span>{" "}
            and{" "}
            <span className="font-medium text-zinc-700">{existingArraysCount} array{existingArraysCount === 1 ? "" : "s"}</span>{" "}
            before checkout.
          </p>
          <p className="mt-1 text-center text-xs text-zinc-400">
            Edit clients and arrays any time in your dashboard.
          </p>
          <div className="mt-8 flex flex-col items-center gap-3">
            <Button onClick={handleComplete} disabled={completing}>
              {completing ? (
                <>
                  <Spinner />
                  Finishing…
                </>
              ) : (
                "Finish setup →"
              )}
            </Button>
            <button
              type="button"
              onClick={() => setAddingMore(true)}
              className="text-sm text-zinc-400 hover:text-zinc-600 focus:outline-none"
            >
              + Add more clients
            </button>
          </div>
        </Card>
      </ScreenLayout>
    );
  }

  // Confirmation sub-screen shown after submitClients succeeds, before completeOnboarding.
  if (confirmSummary) {
    return (
      <ScreenLayout current={4}>
        <Card active>
          <div aria-hidden className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary-100 text-2xl text-primary-600">
            ✓
          </div>
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-zinc-900 text-center">
            Looks good — here&apos;s what we&apos;ll do.
          </h1>
          <p className="mt-2 text-center text-sm text-zinc-500">
            We&apos;ll start generating reports for these clients each quarter.
          </p>
          <ul className="mt-6 space-y-3">
            {confirmSummary.clients.map((c, i) => (
              <li key={i} className="rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3">
                <div className="flex items-start justify-between gap-2">
                  <span className="font-medium text-zinc-900">{c.name}</span>
                  <span className="shrink-0 rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-medium text-primary-700">
                    {c.autopop ? "auto-detect arrays" : `${c.arrayCount} array${c.arrayCount === 1 ? "" : "s"}`}
                  </span>
                </div>
                {c.contact_email && (
                  <div className="mt-1 text-xs text-zinc-500">
                    Reports → {c.contact_email}
                  </div>
                )}
                {!c.contact_email && (
                  <div className="mt-1 text-xs text-amber-600">
                    Without a contact email, this client won&apos;t receive their report.
                  </div>
                )}
              </li>
            ))}
          </ul>
          <div className="mt-8 flex flex-col gap-3 items-center">
            <Button
              onClick={handleComplete}
              disabled={completing}
            >
              {completing ? (
                <>
                  <Spinner />
                  Finishing…
                </>
              ) : (
                "Looks right — finish setup →"
              )}
            </Button>
            <button
              type="button"
              onClick={() => setConfirmSummary(null)}
              disabled={completing}
              className="text-sm text-zinc-400 hover:text-zinc-600 focus:outline-none disabled:opacity-50"
            >
              ← Go back and edit
            </button>
          </div>
        </Card>
      </ScreenLayout>
    );
  }

  return (
    <ScreenLayout current={4}>
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
              Some arrays sum multiple GMP accounts into one (e.g. a site with
              three sub-meters that report separately). Auto-populate creates one
              array per GMP account. If autopop creates arrays you wanted merged,
              fix it in the dashboard after onboarding:
            </p>
            <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs leading-relaxed text-amber-800">
              <li>Open one of the arrays you want to keep</li>
              <li>Click <span className="font-medium">Link a utility account</span></li>
              <li>Add the other arrays&apos; GMP account numbers</li>
              <li>Delete the now-duplicate arrays</li>
            </ol>
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
                  label="Contact email (optional)"
                  type="email"
                  placeholder="reports@mapleridge.org"
                  value={c.contact_email}
                  onChange={(e) => update(c.id, { contact_email: e.target.value })}
                />
                {!c.contact_email.trim() && (
                  <p className="mt-1 text-[11px] text-amber-600">
                    Without a contact email, this client won&apos;t receive their report.
                  </p>
                )}

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
                        The credential the client uses to sign in at
                        greenmountainpower.com. We use this to match captured
                        bills to this client.
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
                                label="NEPOOL-GIS ID"
                                placeholder="53984"
                                value={a.nepool_gis_id}
                                onChange={(e) =>
                                  updateArray(c.id, ai, {
                                    nepool_gis_id: e.target.value,
                                  })
                                }
                              />
                              <p className="mt-1 text-[11px] leading-snug text-zinc-400">
                                The 5-digit asset ID from ISO-NE — required to
                                ship reports. You can add this later if you do
                                not have it now.
                              </p>
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
            onClick={handleSubmitClients}
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
