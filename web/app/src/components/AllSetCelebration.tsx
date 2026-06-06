import { useEffect, useRef, useState } from "react";
import { Modal } from "../ui/Modal";
import { Button } from "../ui/Button";
import type { Account } from "../lib/api";

interface Props {
  account: Account | null;
}

function seenKey(tenantId: string): string {
  return `so_all_set_seen_${tenantId}`;
}

export function AllSetCelebration({ account }: Props) {
  const prevAllSet = useRef<boolean | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!account) return;
    const prev = prevAllSet.current;
    prevAllSet.current = account.all_set;

    // Only fire on the false → true transition.
    if (prev !== false || !account.all_set) return;

    try {
      if (localStorage.getItem(seenKey(account.tenant_id)) === "true") return;
    } catch {
      return;
    }
    setOpen(true);
  }, [account]);

  function dismiss() {
    if (account) {
      try {
        localStorage.setItem(seenKey(account.tenant_id), "true");
      } catch { /* ignore quota */ }
    }
    setOpen(false);
  }

  if (!account) return null;

  const n = account.accounts_count;
  const m = account.clients_count;

  return (
    <Modal
      open={open}
      onClose={dismiss}
      title="You're all set ✓"
      footer={<Button onClick={dismiss}>Got it</Button>}
    >
      <p className="text-zinc-700">
        {n} {n === 1 ? "array" : "arrays"} across {m} {m === 1 ? "client" : "clients"} are ready to go.
        Your first quarterly report will go out automatically on the schedule you picked.
      </p>
      <p className="mt-3 text-xs text-zinc-400">
        You can view and adjust your report schedule on the Automatic Reports tab.
      </p>
    </Modal>
  );
}
