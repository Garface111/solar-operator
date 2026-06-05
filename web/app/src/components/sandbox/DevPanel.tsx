// Floating dev panel for the sandbox. Renders only when the server reports
// SO_DEV_ENABLED=1 — otherwise mounts nothing. Lets you seed fake clients,
// fake logins-with-arrays, unclassified accounts, and wipe all dev rows in
// one click. Lives bottom-right, collapsible, dark theme so it's obviously
// not production chrome.

import { useEffect, useRef, useState } from 'react';
import {
  devStatus,
  devSeedClients,
  devSeedLogin,
  devSeedUnclassified,
  devWipe,
  type DevStatus,
} from '../../lib/api';

interface Props {
  /** Called whenever a seed/wipe action completes so the canvas reloads. */
  onChange: () => void;
  /** List of currently-rendered clients so seed-login can target one. */
  clients: { id: number; name: string }[];
}

export function DevPanel({ onChange, clients }: Props) {
  const [status, setStatus] = useState<DevStatus | null>(null);
  const [open, setOpen] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);

  // Inputs
  const [clientCount, setClientCount] = useState(3);
  const [loginClientId, setLoginClientId] = useState<number | ''>('');
  const [loginUtility, setLoginUtility] = useState<'GMP' | 'VEC' | 'WEC'>('GMP');
  const [loginArrays, setLoginArrays] = useState(3);
  const [uncCount, setUncCount] = useState(2);

  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);

  useEffect(() => {
    devStatus()
      .then((s) => { if (mounted.current) setStatus(s); })
      .catch(() => { /* dev mode probably off — render nothing */ });
  }, []);

  // Auto-pick first client for the login seed once clients arrive
  useEffect(() => {
    if (loginClientId === '' && clients.length > 0) {
      setLoginClientId(clients[0].id);
    }
  }, [clients, loginClientId]);

  if (!status || !status.enabled) return null;

  const append = (msg: string) =>
    setLog((l) => [`${new Date().toLocaleTimeString()}  ${msg}`, ...l].slice(0, 8));

  const run = async (label: string, fn: () => Promise<string>) => {
    setBusy(label);
    try {
      const result = await fn();
      append(`✓ ${label}: ${result}`);
      onChange();
    } catch (err) {
      append(`✗ ${label}: ${err instanceof Error ? err.message : 'failed'}`);
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <div
      className="absolute bottom-4 right-4 z-50 select-none rounded-xl border border-amber-500/40 bg-zinc-900/95 text-zinc-100 shadow-2xl backdrop-blur-sm"
      style={{ width: open ? 320 : 'auto' }}
    >
      {/* Header */}
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 rounded-t-xl bg-gradient-to-r from-amber-600 to-orange-600 px-3 py-2 text-left text-xs font-bold uppercase tracking-wider text-white"
        onClick={() => setOpen((v) => !v)}
        title="Toggle dev panel"
      >
        <span>🛠 Dev Sandbox</span>
        <span className="font-mono text-[10px] font-normal opacity-80">
          {status.dev_clients} [DEV] · {open ? '▾' : '▸'}
        </span>
      </button>

      {open && (
        <div className="space-y-3 p-3 text-xs">
          {/* Seed clients */}
          <Row label="Add fake clients">
            <NumInput value={clientCount} onChange={setClientCount} min={1} max={25} />
            <ActionButton
              busy={busy === 'seed clients'}
              onClick={() => run('seed clients', async () => {
                const r = await devSeedClients(clientCount);
                return `+${r.created.length} clients`;
              })}
            >
              + Clients
            </ActionButton>
          </Row>

          {/* Seed login (with arrays) under a client */}
          <div className="space-y-1.5 rounded-md bg-zinc-800/60 p-2">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-400/80">
              Add login w/ arrays → client
            </div>
            <select
              value={loginClientId}
              onChange={(e) => setLoginClientId(e.target.value === '' ? '' : parseInt(e.target.value, 10))}
              className="w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
            >
              <option value="">— pick client —</option>
              {clients.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
            <div className="flex gap-1.5">
              <select
                value={loginUtility}
                onChange={(e) => setLoginUtility(e.target.value as 'GMP' | 'VEC' | 'WEC')}
                className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
              >
                <option>GMP</option>
                <option>VEC</option>
                <option>WEC</option>
              </select>
              <NumInput value={loginArrays} onChange={setLoginArrays} min={1} max={15} />
              <ActionButton
                busy={busy === 'seed login'}
                disabled={loginClientId === ''}
                onClick={() => run('seed login', async () => {
                  if (loginClientId === '') throw new Error('pick a client');
                  const r = await devSeedLogin(loginClientId, loginUtility, loginArrays);
                  return `+${r.arrays.length} arrays under ${loginUtility} #${r.customer_number}`;
                })}
              >
                + Login
              </ActionButton>
            </div>
          </div>

          {/* Seed unclassified */}
          <Row label="Add unclassified accounts">
            <NumInput value={uncCount} onChange={setUncCount} min={1} max={15} />
            <ActionButton
              busy={busy === 'seed unclassified'}
              onClick={() => run('seed unclassified', async () => {
                const r = await devSeedUnclassified(uncCount, 'GMP');
                return `+${r.created.length} accounts`;
              })}
            >
              + Floating
            </ActionButton>
          </Row>

          {/* Wipe */}
          <button
            type="button"
            disabled={busy === 'wipe'}
            onClick={() => {
              if (!confirm('Soft-delete every [DEV]-prefixed client / array / account?')) return;
              void run('wipe', async () => {
                const r = await devWipe();
                return `−${r.clients_removed}c / −${r.arrays_removed}a / −${r.accounts_removed}acc`;
              });
            }}
            className="w-full rounded-md border border-red-500/40 bg-red-950/60 px-3 py-1.5 text-xs font-semibold text-red-200 transition-colors hover:bg-red-900/80 disabled:opacity-50"
          >
            {busy === 'wipe' ? '…wiping' : '🧹 Wipe all [DEV] rows'}
          </button>

          {/* Log */}
          {log.length > 0 && (
            <div className="space-y-0.5 rounded-md bg-zinc-950/80 p-2 font-mono text-[10px] leading-tight text-zinc-400">
              {log.map((line, i) => (
                <div key={i} className="truncate">{line}</div>
              ))}
            </div>
          )}

          <div className="border-t border-zinc-700 pt-2 text-[10px] text-zinc-500">
            Tenant: <span className="font-mono">{status.tenant_id}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5 rounded-md bg-zinc-800/60 p-2">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-amber-400/80">
        {label}
      </div>
      <div className="flex gap-1.5">{children}</div>
    </div>
  );
}

function NumInput({ value, onChange, min, max }: { value: number; onChange: (n: number) => void; min: number; max: number }) {
  return (
    <input
      type="number"
      value={value}
      min={min}
      max={max}
      onChange={(e) => {
        const n = parseInt(e.target.value, 10);
        if (!isNaN(n)) onChange(Math.max(min, Math.min(max, n)));
      }}
      className="w-16 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
    />
  );
}

function ActionButton({
  children, onClick, busy, disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  busy?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || disabled}
      className="flex-1 rounded-md bg-amber-600 px-2 py-1 text-xs font-semibold text-white transition-colors hover:bg-amber-500 disabled:bg-zinc-700 disabled:text-zinc-400"
    >
      {busy ? '…' : children}
    </button>
  );
}
