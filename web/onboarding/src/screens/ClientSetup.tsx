import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ScreenLayout } from "../ui/ScreenLayout";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";

export type ClientSetupMode = "auto" | "manual";

export interface ClientDraftEntry {
  name: string;
  contact_email?: string;
  setup_mode: ClientSetupMode;
  /** When setup_mode === "auto", this is the operator's estimate of how many
   *  arrays this client has — used only for upfront pricing display. The real
   *  array count gets locked in once the extension auto-populates from the
   *  utility portal post-payment. */
  estimated_arrays?: number;
  arrays: { name: string; nepool_gis_id?: string }[];
}

export const SO_CLIENTS_DRAFT_KEY = "so_clients_draft";

interface ArrayDraft {
  name: string;
  nepool_gis_id: string;
}

interface ClientDraft {
  id: number;
  name: string;
  contact_email: string;
  setup_mode: ClientSetupMode;
  estimated_arrays: number;
  arrays: ArrayDraft[];
}

let nextId = 1;

function emptyClient(): ClientDraft {
  return {
    id: nextId++,
    name: "",
    contact_email: "",
    setup_mode: "auto",
    estimated_arrays: 1,
    arrays: [],
  };
}

function loadDraft(): ClientDraft[] {
  try {
    const raw = sessionStorage.getItem(SO_CLIENTS_DRAFT_KEY);
    if (!raw) return [emptyClient()];
    const saved: ClientDraftEntry[] = JSON.parse(raw);
    return saved.map((c) => ({
      id: nextId++,
      name: c.name,
      contact_email: c.contact_email ?? "",
      setup_mode: c.setup_mode ?? "auto",
      estimated_arrays:
        c.estimated_arrays && c.estimated_arrays > 0 ? c.estimated_arrays : 1,
      arrays: (c.arrays ?? []).map((a) => ({
        name: a.name,
        nepool_gis_id: a.nepool_gis_id ?? "",
      })),
    }));
  } catch {
    return [emptyClient()];
  }
}

export default function ClientSetup() {
  const navigate = useNavigate();
  const [clients, setClients] = useState<ClientDraft[]>(loadDraft);

  function update(id: number, patch: Partial<ClientDraft>) {
    setClients((cs) => cs.map((c) => (c.id === id ? { ...c, ...patch } : c)));
  }
  function addClient() {
    setClients((cs) => [...cs, emptyClient()]);
  }
  function removeClient(id: number) {
    setClients((cs) => (cs.length === 1 ? cs : cs.filter((c) => c.id !== id)));
  }
  function addArray(id: number) {
    setClients((cs) =>
      cs.map((c) =>
        c.id === id ? { ...c, arrays: [...c.arrays, { name: "", nepool_gis_id: "" }] } : c,
      ),
    );
  }
  function updateArray(id: number, ai: number, patch: Partial<ArrayDraft>) {
    setClients((cs) =>
      cs.map((c) =>
        c.id === id
          ? { ...c, arrays: c.arrays.map((a, i) => (i === ai ? { ...a, ...patch } : a)) }
          : c,
      ),
    );
  }
  function removeArray(id: number, ai: number) {
    setClients((cs) =>
      cs.map((c) => (c.id === id ? { ...c, arrays: c.arrays.filter((_, i) => i !== ai) } : c)),
    );
  }

  const allNamed = clients.every((c) => c.name.trim().length >= 1);
  /** Array count used for pricing estimate.
   *  Auto-mode clients contribute their `estimated_arrays`; manual-mode clients
   *  contribute their actually-named arrays. */
  const totalArrays = clients.reduce((n, c) => {
    if (c.setup_mode === "auto") {
      return n + Math.max(0, c.estimated_arrays);
    }
    return n + c.arrays.filter((a) => a.name.trim()).length;
  }, 0);
  const canContinue = allNamed && totalArrays > 0;

  function handleContinue() {
    if (!canContinue) return;
    const draft: ClientDraftEntry[] = clients.map((c) => ({
      name: c.name.trim(),
      contact_email: c.contact_email.trim() || undefined,
      setup_mode: c.setup_mode,
      estimated_arrays:
        c.setup_mode === "auto" ? Math.max(1, c.estimated_arrays) : undefined,
      arrays:
        c.setup_mode === "manual"
          ? c.arrays
              .filter((a) => a.name.trim())
              .map((a) => ({
                name: a.name.trim(),
                nepool_gis_id: a.nepool_gis_id.trim() || undefined,
              }))
          : [],
    }));
    sessionStorage.setItem(SO_CLIENTS_DRAFT_KEY, JSON.stringify(draft));
    navigate("/plan");
  }

  return (
    <ScreenLayout current={2}>
      <Card active>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">
          Tell us about your clients.
        </h1>
        <p className="mt-2 text-sm text-zinc-500">
          Add each client you generate reports for. The fastest path is{" "}
          <span className="font-semibold text-zinc-700">auto-detect</span> —
          we&apos;ll pull your arrays from your utility portal the first time
          you log in. You can also enter them manually now.
        </p>

        <div className="mt-8 space-y-6">
          {clients.map((c, ci) => (
            <div key={c.id} className="rounded-xl border border-zinc-200 p-5">
              <div className="mb-4 flex items-center justify-between">
                <span className="text-sm font-semibold text-zinc-700">
                  Client {ci + 1}
                </span>
                {clients.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeClient(c.id)}
                    className="rounded text-xs font-medium text-zinc-400 transition-colors hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                  >
                    Remove
                  </button>
                )}
              </div>

              <div className="space-y-4">
                <Input
                  id={`cs-name-${c.id}`}
                  label="Client name"
                  placeholder="Maple Ridge HOA"
                  value={c.name}
                  onChange={(e) => update(c.id, { name: e.target.value })}
                />
                <Input
                  id={`cs-email-${c.id}`}
                  label="Contact email (optional)"
                  type="email"
                  placeholder="reports@mapleridge.org"
                  value={c.contact_email}
                  onChange={(e) => update(c.id, { contact_email: e.target.value })}
                />
                {!c.contact_email.trim() && (
                  <p className="text-[11px] text-amber-600">
                    Without a contact email, this client won&apos;t receive their report.
                  </p>
                )}

                {/* Setup mode — auto-detect (default) vs. manual entry */}
                <div>
                  <p className="mb-2 text-xs font-medium text-zinc-600">
                    How would you like to add this client&apos;s arrays?
                  </p>
                  <div
                    role="radiogroup"
                    aria-label="Array setup mode"
                    className="grid gap-2 sm:grid-cols-2"
                  >
                    <label
                      className={`flex cursor-pointer items-start gap-3 rounded-xl border p-3 text-sm transition-colors ${
                        c.setup_mode === "auto"
                          ? "border-primary-500 bg-primary-50"
                          : "border-zinc-200 bg-white hover:border-zinc-300"
                      }`}
                    >
                      <input
                        type="radio"
                        name={`cs-mode-${c.id}`}
                        value="auto"
                        checked={c.setup_mode === "auto"}
                        onChange={() => update(c.id, { setup_mode: "auto" })}
                        className="mt-1 h-4 w-4 accent-primary-600"
                      />
                      <span>
                        <span className="block font-semibold text-zinc-800">
                          Auto-detect from utility portal
                        </span>
                        <span className="mt-0.5 block text-xs text-zinc-500">
                          We&apos;ll pull this client&apos;s arrays automatically
                          when you log into your utility portal after checkout.
                        </span>
                      </span>
                    </label>
                    <label
                      className={`flex cursor-pointer items-start gap-3 rounded-xl border p-3 text-sm transition-colors ${
                        c.setup_mode === "manual"
                          ? "border-primary-500 bg-primary-50"
                          : "border-zinc-200 bg-white hover:border-zinc-300"
                      }`}
                    >
                      <input
                        type="radio"
                        name={`cs-mode-${c.id}`}
                        value="manual"
                        checked={c.setup_mode === "manual"}
                        onChange={() => update(c.id, { setup_mode: "manual" })}
                        className="mt-1 h-4 w-4 accent-primary-600"
                      />
                      <span>
                        <span className="block font-semibold text-zinc-800">
                          Enter arrays manually
                        </span>
                        <span className="mt-0.5 block text-xs text-zinc-500">
                          Type each array name and NEPOOL-GIS ID yourself.
                        </span>
                      </span>
                    </label>
                  </div>
                </div>

                {c.setup_mode === "auto" ? (
                  <div>
                    <Input
                      id={`cs-estarr-${c.id}`}
                      label="Approximately how many arrays does this client have?"
                      type="number"
                      min={1}
                      value={String(c.estimated_arrays)}
                      onChange={(e) => {
                        const n = parseInt(e.target.value, 10);
                        update(c.id, {
                          estimated_arrays: Number.isFinite(n) && n > 0 ? n : 1,
                        });
                      }}
                    />
                    <p className="mt-1 text-[11px] text-zinc-500">
                      We use this only to size your subscription. The exact
                      count locks in when we auto-detect from your utility
                      portal.
                    </p>
                  </div>
                ) : (
                  <div>
                    <p className="mb-2 text-xs font-medium text-zinc-600">
                      Arrays
                    </p>
                    <div className="space-y-3">
                      {c.arrays.map((a, ai) => (
                        <div key={ai} className="flex flex-col gap-2 sm:flex-row sm:items-end">
                          <div className="flex-1">
                            <Input
                              id={`cs-arr-name-${c.id}-${ai}`}
                              label="Array name"
                              placeholder="South Field"
                              value={a.name}
                              onChange={(e) => updateArray(c.id, ai, { name: e.target.value })}
                            />
                          </div>
                          <div className="flex-1">
                            <Input
                              id={`cs-arr-gis-${c.id}-${ai}`}
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
                            className="self-end rounded px-2 py-2 text-zinc-400 hover:text-red-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 sm:mb-1"
                          >
                            ✕
                          </button>
                        </div>
                      ))}
                    </div>
                    <button
                      type="button"
                      onClick={() => addArray(c.id)}
                      className="mt-3 rounded text-sm font-medium text-primary-600 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
                    >
                      + Add array
                    </button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>

        <button
          type="button"
          onClick={addClient}
          className="mt-6 rounded text-sm font-medium text-primary-600 hover:text-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
        >
          + Add another client
        </button>

        {totalArrays > 0 && (
          <div className="mt-6 rounded-xl border border-primary-200 bg-primary-50 px-4 py-3 text-sm">
            <span className="font-medium text-primary-800">
              {clients.length} {clients.length === 1 ? "client" : "clients"},{" "}
              {totalArrays} {totalArrays === 1 ? "array" : "arrays"}
            </span>
            <span className="ml-2 text-primary-700">
              — ${totalArrays * 45}/month after setup
            </span>
          </div>
        )}

        <div className="mt-8 flex items-center justify-between">
          <Button variant="ghost" onClick={() => navigate("/info")}>
            ← Back
          </Button>
          <Button onClick={handleContinue} disabled={!canContinue}>
            Review pricing →
          </Button>
        </div>
      </Card>
    </ScreenLayout>
  );
}
