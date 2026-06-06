import { useState } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Toggle } from "../ui/Toggle";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { createClient, ConflictError, type ClientRow } from "../lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated: (client: ClientRow) => void;
}

export function AddClientModal({ open, onClose, onCreated }: Props) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [autopop, setAutopop] = useState(true);
  const [gmpLogin, setGmpLogin] = useState("");
  const [vecAutopop, setVecAutopop] = useState(true);
  const [vecLogin, setVecLogin] = useState("");
  const [saving, setSaving] = useState(false);

  function reset() {
    setName("");
    setEmail("");
    setAutopop(true);
    setGmpLogin("");
    setVecAutopop(true);
    setVecLogin("");
  }

  const valid =
    name.trim().length >= 1 &&
    (!email.trim() || EMAIL_RE.test(email.trim())) &&
    (!autopop || gmpLogin.trim().length >= 1) &&
    (!vecAutopop || vecLogin.trim().length >= 1);

  async function handleCreate() {
    if (!valid || saving) return;
    setSaving(true);
    const login = gmpLogin.trim();
    const looksLikeEmail = EMAIL_RE.test(login);
    const vLogin = vecLogin.trim();
    const vLooksLikeEmail = EMAIL_RE.test(vLogin);
    try {
      const client = await createClient({
        name: name.trim(),
        contact_email: email.trim() || null,
        gmp_autopopulate: autopop,
        gmp_email: autopop && login && looksLikeEmail ? login : null,
        gmp_username: autopop && login && !looksLikeEmail ? login : null,
        vec_autopopulate: vecAutopop,
        vec_email: vecAutopop && vLogin && vLooksLikeEmail ? vLogin : null,
        vec_username: vecAutopop && vLogin && !vLooksLikeEmail ? vLogin : null,
      });
      onCreated(client);
      reset();
      onClose();
      toast.success(`Added ${client.name}`);
    } catch (err) {
      if (err instanceof ConflictError && err.detail?.existing_client_id) {
        // Login already on file — jump the operator to the existing client
        // instead of creating a dupe. Sublime: zero second-guessing.
        const existingName = err.detail.existing_client_name || "this client";
        toast.success(
          `Already on file as "${existingName}" — opening it.`,
        );
        reset();
        onClose();
        // Best-effort scroll to the already-mounted card.
        window.setTimeout(() => {
          const el = document.getElementById(
            `client-${err.detail.existing_client_id}`,
          );
          if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
        }, 150);
      } else {
        toast.error(
          err instanceof Error ? err.message : "Couldn't add the client",
        );
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!saving) {
          reset();
          onClose();
        }
      }}
      title="Add a client"
      footer={
        <>
          <Button
            variant="ghost"
            onClick={() => {
              reset();
              onClose();
            }}
            disabled={saving}
          >
            I&apos;m done
          </Button>
          <Button onClick={handleCreate} disabled={!valid || saving}>
            {saving ? (
              <>
                <Spinner />
                Adding…
              </>
            ) : (
              "Add client"
            )}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Input
          id="new-client-name"
          label="Client name"
          autoFocus
          placeholder="Maple Ridge HOA"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Input
          id="new-client-email"
          label="Contact email (optional)"
          type="email"
          placeholder="reports@mapleridge.org"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        {!email.trim() && (
          <p className="-mt-2 text-[11px] text-amber-600">
            Without a contact email, this client won&apos;t receive their report.
          </p>
        )}
        <div className="rounded-xl bg-zinc-50 px-4 py-3">
          <Toggle
            id="new-client-autopop"
            checked={autopop}
            onChange={setAutopop}
            label="GMP — auto-populate arrays from portal"
          />
          {autopop && (
            <div className="mt-4">
              <Input
                id="new-client-gmp"
                label="GMP login (email or username)"
                placeholder="client@gmail.com or jdoe"
                value={gmpLogin}
                onChange={(e) => setGmpLogin(e.target.value)}
              />
              <p className="mt-1.5 text-xs text-zinc-500">
                The credential the client uses to sign in at
                greenmountainpower.com. We use this to match captured bills to
                this client.
              </p>
            </div>
          )}
        </div>
        <div className="rounded-xl bg-zinc-50 px-4 py-3">
          <Toggle
            id="new-client-vec-autopop"
            checked={vecAutopop}
            onChange={setVecAutopop}
            label="VEC — auto-populate arrays from portal"
          />
          {vecAutopop && (
            <div className="mt-4">
              <Input
                id="new-client-vec"
                label="VEC login (email or username)"
                placeholder="client@gmail.com or jdoe"
                value={vecLogin}
                onChange={(e) => setVecLogin(e.target.value)}
              />
              <p className="mt-1.5 text-xs text-zinc-500">
                The credential the client uses to sign in at
                vermontelectric.coop. We use this to match captured bills to
                this client.
              </p>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
