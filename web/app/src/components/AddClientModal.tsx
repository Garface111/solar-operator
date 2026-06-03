import { useState } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Toggle } from "../ui/Toggle";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { createClient, type ClientRow } from "../lib/api";

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
  const [saving, setSaving] = useState(false);

  function reset() {
    setName("");
    setEmail("");
    setAutopop(true);
    setGmpLogin("");
  }

  const valid =
    name.trim().length >= 1 &&
    (!email.trim() || EMAIL_RE.test(email.trim())) &&
    (!autopop || gmpLogin.trim().length >= 1);

  async function handleCreate() {
    if (!valid || saving) return;
    setSaving(true);
    const login = gmpLogin.trim();
    const looksLikeEmail = EMAIL_RE.test(login);
    try {
      const client = await createClient({
        name: name.trim(),
        contact_email: email.trim() || null,
        gmp_autopopulate: autopop,
        gmp_email: autopop && login && looksLikeEmail ? login : null,
        gmp_username: autopop && login && !looksLikeEmail ? login : null,
      });
      onCreated(client);
      reset();
      onClose();
      toast.success(`Added ${client.name}`);
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Couldn't add the client",
      );
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
            Cancel
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
          label="Contact email (where reports go)"
          type="email"
          placeholder="reports@mapleridge.org"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <div className="rounded-xl bg-zinc-50 px-4 py-3">
          <Toggle
            id="new-client-autopop"
            checked={autopop}
            onChange={setAutopop}
            label="Auto-populate arrays from GMP"
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
                When this client logs into GMP through the extension, we&apos;ll
                add their arrays automatically.
              </p>
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
