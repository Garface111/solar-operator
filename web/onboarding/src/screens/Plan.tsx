import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { createCheckout, setToken, type ClientSeedPayload } from "../lib/onboarding";

interface OperatorInfo {
  email: string;
  fullName: string;
  company?: string;
}

interface ArrayDraft {
  name: string;
  nepool_gis_id: string;
}

interface ClientDraft {
  id: number;
  name: string;
  contact_email: string;
  arrays: ArrayDraft[];
}

let nextDraftId = 1;
function blankClient(): ClientDraft {
  return { id: nextDraftId++, name: "", contact_email: "", arrays: [] };
}

type PlanView = "choose" | "path-a" | "path-a-review";

const ARRAY_PRICE = 45;
const SETUP_FEE = 250;

export default function Plan() {
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();

  const info = location.state as OperatorInfo | null;

  const [view, setView] = useState<PlanView>("choose");
  const [estimate, setEstimate] = useState(1);
  const [clients, setClients] = useState<ClientDraft[]>(() => [blankClient()]);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!info?.email) {
      navigate("/info", { replace: true });
    }
  }, []);

  if (!info?.email) return null;

  function updateClient(id: number, patch: Partial<ClientDraft>) {
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
          ? { ...c, arrays: c.arrays.map((a, i) => (i === idx ? { ...a, ...patch } : a)) }
          : c,
      ),
    );
  }
  function removeArray(id: number, idx: number) {
    setClients((cs) =>
      cs.map((c) =>
        c.id === id ? { ...c, arrays: c.arrays.filter((_, i) => i !== idx) } : c,
      ),
    );
  }

  const clientsValid = clients.every((c) => c.name.trim().length >= 1);
  const totalArrays = clients.reduce(
    (sum, c) => sum + c.arrays.filter((a) => a.name.trim()).length,
    0,
  );

  async function goToStripe(
    payload: { array_count?: number; clients?: ClientSeedPayload[] },
  ) {
    // info is guaranteed non-null here: useEffect redirects if it's null and
    // we return null early, but TypeScript doesn't narrow through useEffect.
    const op = info!;
    setSubmitting(true);
    try {
      const { checkout_url, onboarding_token } = await createCheckout({
        full_name: op.fullName,
        email: op.email,
        company: op.company,
        ...payload,
      });
      setToken(onboarding_token);
      window.location.href = checkout_url;
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : "Couldn't reach checkout. Check your connection and try again.",
      );
      setSubmitting(false);
    }
  }

  // Path A: inline client + array form
  if (view === "path-a") {
    return (
      <ScreenLayout current={2}>
        <Card active>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
            Add your clients and arrays.
          </h1>
          <p className="mt-2 text-sm text-zinc-500">
            Enter them now to lock in your exact monthly bill. You can add utility
            credentials and NEPOOL-GIS IDs after payment.
          </p>

          <div className="mt-8 space-y-6">
            {clients.map((c, idx) => (
              <div key={c.id} className="rounded-xl border border-zinc-200 p-5">
                <div className="mb-4 flex items-center justify-between">
                  <span className="text-sm font-semibold text-zinc-700">
                    Client {idx + 1}
                  </span>
                  {clients.length > 1 && (
                    <button
                      type="button"
                      onClick={() => removeClient(c.id)}
                      className="rounded text-xs font-medium text-zinc-400 transition-colors duration-150 ease-in-out hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2"
                    >
                      Remove
                    </button>
                  )}
                </div>

                <div className="space-y-4">
                  <Input
                    id={`plan-name-${c.id}`}
                    label="Client name"
                    placeholder="Maple Ridge HOA"
                    value={c.name}
                    onChange={(e) => updateClient(c.id, { name: e.target.value })}
                  />
                  <Input
                    id={`plan-email-${c.id}`}
                    label="Contact email (optional)"
                    type="email"
                    placeholder="reports@mapleridge.org"
                    value={c.contact_email}
                    onChange={(e) => updateClient(c.id, { contact_email: e.target.value })}
                  />
                  {!c.contact_email.trim() && (
                    <p className="mt-1 text-[11px] text-amber-600">
                      Without a contact email, this client won&apos;t receive their report.
                    </p>
                  )}

                  <div>
                    <p className="mb-2 text-xs font-medium text-zinc-600">Arrays</p>
                    <div className="space-y-3">
                      {c.arrays.map((a, ai) => (
                        <div
                          key={ai}
                          className="flex flex-col gap-2 sm:flex-row sm:items-end"
                        >
                          <div className="flex-1">
                            <Input
                              id={`plan-arr-name-${c.id}-${ai}`}
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
                              id={`plan-arr-gis-${c.id}-${ai}`}
                              label="NEPOOL-GIS ID (optional)"
                              placeholder="53984"
                              value={a.nepool_gis_id}
                              onChange={(e) =>
                                updateArray(c.id, ai, { nepool_gis_id: e.target.value })
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

          <div className="mt-8 flex items-center justify-between">
            <Button
              variant="ghost"
              onClick={() => setView("choose")}
              disabled={submitting}
            >
              ← Back
            </Button>
            <Button
              onClick={() => setView("path-a-review")}
              disabled={!clientsValid || totalArrays < 1 || submitting}
            >
              Review my bill →
            </Button>
          </div>
        </Card>
      </ScreenLayout>
    );
  }

  // Path A: billing review before Stripe Checkout
  if (view === "path-a-review") {
    const quantity = Math.max(1, totalArrays);
    const monthly = quantity * ARRAY_PRICE;
    const todayTotal = SETUP_FEE + monthly;
    const now = new Date();
    const nextDate = new Date(now.getFullYear(), now.getMonth() + 1, 1);
    const nextMonth = nextDate.toLocaleDateString("en-US", {
      month: "long",
      year: "numeric",
    });

    const clientSeeds: ClientSeedPayload[] = clients.map((c) => ({
      name: c.name.trim(),
      contact_email: c.contact_email.trim() || undefined,
      arrays: c.arrays
        .filter((a) => a.name.trim())
        .map((a) => ({
          name: a.name.trim(),
          nepool_gis_id: a.nepool_gis_id.trim() || undefined,
        })),
    }));

    return (
      <ScreenLayout current={2}>
        <Card active>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
            Your bill
          </h1>
          <p className="mt-2 text-sm text-zinc-500">
            Here&apos;s exactly what you&apos;ll pay. No surprises.
          </p>

          <div className="mt-6 rounded-xl border border-zinc-200 bg-zinc-50 p-5 space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span className="text-zinc-600">One-time setup</span>
              <span className="font-medium text-zinc-900">${SETUP_FEE}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-zinc-600">
                Monthly ({quantity} {quantity === 1 ? "array" : "arrays"} × ${ARRAY_PRICE})
              </span>
              <span className="font-medium text-zinc-900">${monthly}/month</span>
            </div>
            <div className="border-t border-zinc-200 pt-3 flex items-center justify-between text-sm font-medium">
              <span className="text-zinc-700">Charged today</span>
              <span className="text-zinc-900">${todayTotal}</span>
            </div>
            <p className="text-xs text-zinc-500">
              Then ${monthly}/month starting {nextMonth}.
            </p>
          </div>

          <ul className="mt-6 space-y-2">
            {clients.map((c, i) => {
              const arrCount = c.arrays.filter((a) => a.name.trim()).length;
              return (
                <li
                  key={i}
                  className="rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-zinc-900">{c.name.trim()}</span>
                    <span className="shrink-0 rounded-full bg-primary-100 px-2 py-0.5 text-[11px] font-medium text-primary-700">
                      {arrCount} {arrCount === 1 ? "array" : "arrays"}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>

          <div className="mt-8 flex items-center justify-between">
            <Button
              variant="ghost"
              onClick={() => setView("path-a")}
              disabled={submitting}
            >
              ← Back, adjust
            </Button>
            <Button
              onClick={() => goToStripe({ clients: clientSeeds })}
              disabled={submitting}
            >
              {submitting ? (
                <>
                  <Spinner />
                  Redirecting…
                </>
              ) : (
                "Continue to payment →"
              )}
            </Button>
          </div>
        </Card>
      </ScreenLayout>
    );
  }

  // Default: choose view — two cards, Path A and Path B
  return (
    <ScreenLayout current={2}>
      <div className="space-y-4 sm:space-y-0 sm:grid sm:grid-cols-2 sm:gap-4">
        {/* Card A: know your arrays */}
        <Card active className="flex flex-col">
          <h2 className="text-lg font-semibold text-zinc-900">
            I know my arrays
          </h2>
          <p className="mt-2 text-sm text-zinc-500 flex-1">
            Enter your clients and arrays now to lock in your exact monthly bill
            before paying.
          </p>
          <Button
            className="mt-6 w-full"
            onClick={() => setView("path-a")}
            disabled={submitting}
          >
            Add your clients &amp; arrays →
          </Button>
        </Card>

        {/* Card B: estimate */}
        <Card className="flex flex-col">
          <h2 className="text-lg font-semibold text-zinc-900">
            I&apos;ll estimate for now
          </h2>
          <p className="mt-2 text-sm text-zinc-500">
            Quick estimate — true-up later.{" "}
            <span className="font-medium text-zinc-700">
              We&apos;ll never charge more without telling you first.
            </span>
          </p>
          <div className="mt-6">
            <label
              htmlFor="estimate-count"
              className="block text-sm font-medium text-zinc-700"
            >
              About how many arrays do you serve today?
            </label>
            <input
              id="estimate-count"
              type="number"
              min={1}
              value={estimate}
              onChange={(e) =>
                setEstimate(Math.max(1, parseInt(e.target.value, 10) || 1))
              }
              className="mt-1.5 block w-28 rounded-xl border border-zinc-300 px-3 py-2 text-sm text-zinc-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/30"
            />
            <p className="mt-1.5 text-xs text-zinc-500">
              You can adjust later — your card won&apos;t be charged extra
              without notice.
            </p>
          </div>
          <Button
            variant="secondary"
            className="mt-6 w-full"
            onClick={() => goToStripe({ array_count: estimate })}
            disabled={submitting}
          >
            {submitting ? (
              <>
                <Spinner />
                Redirecting…
              </>
            ) : (
              `Continue with ${estimate} ${estimate === 1 ? "array" : "arrays"} →`
            )}
          </Button>
        </Card>
      </div>

      <div className="mt-4">
        <Button
          variant="ghost"
          onClick={() => navigate("/info", { state: info })}
          disabled={submitting}
        >
          ← Back to your info
        </Button>
      </div>
    </ScreenLayout>
  );
}
