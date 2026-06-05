import { useState } from "react";
import { Button } from "../../ui/Button";
import { useToast } from "../../ui/Toast";
import { cancelTrial, clearSession } from "../../lib/api";

interface Props {
  onCancelled: () => void;
}

export function DangerZoneCard({ onCancelled }: Props) {
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [loading, setLoading] = useState(false);

  async function handleCancel() {
    setLoading(true);
    try {
      await cancelTrial();
      clearSession();
      toast.success("Trial cancelled — you won't be charged.");
      onCancelled();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Cancellation failed");
      setLoading(false);
      setConfirming(false);
    }
  }

  return (
    <div>
      <div className="mb-4 flex items-center gap-3">
        <div className="h-px flex-1 bg-zinc-200" />
        <span className="text-xs font-medium uppercase tracking-wide text-zinc-400">
          Danger zone
        </span>
        <div className="h-px flex-1 bg-zinc-200" />
      </div>

      <div className="rounded-xl border border-red-200 bg-white p-6">
        <h2 className="mb-1 text-base font-semibold text-zinc-800">Cancel trial</h2>
        <p className="mb-4 text-sm text-zinc-500">
          You won&apos;t be charged. Your data will be removed. This can&apos;t be undone.
        </p>
        {!confirming ? (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="text-sm text-red-600 underline underline-offset-2 hover:text-red-800 focus:outline-none"
          >
            Cancel my trial
          </button>
        ) : (
          <div className="flex items-center gap-3">
            <Button variant="secondary" onClick={() => setConfirming(false)} disabled={loading}>
              Keep trial
            </Button>
            <Button variant="primary" onClick={handleCancel} disabled={loading}>
              {loading ? "Cancelling…" : "Yes, cancel my trial"}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
