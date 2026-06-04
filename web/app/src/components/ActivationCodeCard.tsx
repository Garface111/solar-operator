import { useState } from "react";
import { Card } from "../ui/Card";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/Toast";
import { CopyButton } from "./CopyButton";

interface Props {
  tenantKey: string | null;
  onKeyRegenerated?: (newKey: string) => void;
}

async function regenKey(): Promise<string> {
  const token = localStorage.getItem("so_session") ?? sessionStorage.getItem("so_session");
  const res = await fetch("/v1/account/regen-key", {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.detail ?? `Request failed (${res.status})`);
  }
  const data = await res.json();
  return data.tenant_key as string;
}

/**
 * Shows the tenant activation code the customer pastes into the Chrome
 * extension's options page so captures route to their account.
 */
export function ActivationCodeCard({ tenantKey, onKeyRegenerated }: Props) {
  const toast = useToast();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [regenerating, setRegenerating] = useState(false);

  async function handleRegen() {
    setRegenerating(true);
    try {
      const newKey = await regenKey();
      onKeyRegenerated?.(newKey);
      setConfirmOpen(false);
      toast.success("Activation code regenerated — paste the new code into the extension.");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Couldn't regenerate the code");
    } finally {
      setRegenerating(false);
    }
  }

  return (
    <Card>
      <h2 className="text-lg font-semibold tracking-tight text-zinc-900">
        Extension activation code
      </h2>
      <p className="mt-1 text-sm text-zinc-500">
        Paste this into the Solar Operator Chrome extension&apos;s options page to
        connect it to your account.
      </p>
      <p className="mt-1 text-xs font-medium text-amber-700">
        Treat this like a password — anyone with this code can send data to your account.
      </p>

      {tenantKey ? (
        <div className="mt-4 flex items-center gap-2 rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3">
          <code className="flex-1 select-all break-all font-mono text-sm text-zinc-800">
            {tenantKey}
          </code>
          <CopyButton value={tenantKey} label="Copy code" />
        </div>
      ) : (
        <p className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          No activation code on file. Email support@solaroperator.org and
          we&apos;ll sort it out.
        </p>
      )}

      <ol className="mt-4 space-y-1.5 text-xs text-zinc-500">
        <li>1. Open the Solar Operator extension and click &ldquo;Options&rdquo;.</li>
        <li>2. Paste the code above into the activation field and save.</li>
        <li>
          3. Log into Green Mountain Power as usual — captures now flow to your
          account automatically.
        </li>
      </ol>

      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={() => setConfirmOpen(true)}
          className="text-xs font-medium text-zinc-400 underline-offset-2 hover:text-zinc-600 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-1"
        >
          Regenerate code
        </button>
      </div>

      <Modal
        open={confirmOpen}
        onClose={() => !regenerating && setConfirmOpen(false)}
        title="Regenerate activation code?"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={regenerating}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleRegen} disabled={regenerating}>
              {regenerating ? (
                <>
                  <Spinner />
                  Regenerating…
                </>
              ) : (
                "Regenerate"
              )}
            </Button>
          </>
        }
      >
        <p className="text-sm text-zinc-700">
          Your current code will stop working immediately. You&apos;ll need to
          paste the new code into the extension&apos;s Options page before
          captures can resume.
        </p>
      </Modal>
    </Card>
  );
}
