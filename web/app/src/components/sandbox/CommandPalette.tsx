import { type Dispatch, type SetStateAction, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useReactFlow, type Node } from '@xyflow/react';
import { useCanvasActions } from './canvasContext';
import type { ClientNodeData } from './ClientNode';
import type { UnclassifiedNodeData } from './UnclassifiedAccountNode';

// ── Types ───────────────────────────────────────────────────────────────────

interface CommandItem {
  id: string;
  label: string;
  sublabel?: string;
  group: string;
  icon: string;
  action: () => void;
}

interface Props {
  nodes: Node[];
  setNodes: Dispatch<SetStateAction<Node[]>>;
  onAddClient: () => void;
  onAutoArrange: () => void;
  onFitView: () => void;
}

// ── Constants ────────────────────────────────────────────────────────────────

const RECENT_KEY = 'so:cmdk:recent';
const MAX_RECENT = 5;

// ── Fuzzy match ──────────────────────────────────────────────────────────────

function fuzzyScore(query: string, text: string): number {
  const q = query.toLowerCase().trim();
  const t = text.toLowerCase();
  if (!q) return 1;
  if (t === q) return 200;
  if (t.startsWith(q)) return 150;
  if (t.split(/\s+/).some((w) => w.startsWith(q))) return 100;
  // Subsequence match
  let qi = 0;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) qi++;
  }
  return qi === q.length ? 50 : 0;
}

// ── Recent storage ───────────────────────────────────────────────────────────

function loadRecent(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) ?? '[]') as string[];
  } catch {
    return [];
  }
}

function saveRecent(id: string) {
  const prev = loadRecent().filter((r) => r !== id);
  const next = [id, ...prev].slice(0, MAX_RECENT);
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // quota exceeded — ignore
  }
}

// ── Component ────────────────────────────────────────────────────────────────

export function CommandPalette({ nodes, setNodes, onAddClient, onAutoArrange, onFitView }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const actions = useCanvasActions();
  const { fitView } = useReactFlow();

  const jumpToNode = useCallback(
    (id: string) => {
      setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === id })));
      fitView({ nodes: [{ id }], padding: 0.5, duration: 400, maxZoom: 1.2 });
    },
    [fitView, setNodes],
  );

  // ── Global keybind ─────────────────────────────────────────────────────────

  useEffect(() => {
    function handler(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setOpen(true);
        setQuery('');
        setActiveIdx(0);
        return;
      }
      if (e.key === 'Escape' && open) {
        // Mark consumed so the fullscreen Esc handler in ClientsTab defers to us.
        e.preventDefault();
        setOpen(false);
      }
    }
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open]);

  // Focus input on open
  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  // ── Build command list ─────────────────────────────────────────────────────

  const allCommands = useMemo<CommandItem[]>(() => {
    const items: CommandItem[] = [];

    // Static actions
    items.push({
      id: 'add-client',
      label: 'Add Client',
      sublabel: 'Open portal picker',
      group: 'Actions',
      icon: '+',
      action: onAddClient,
    });

    items.push({
      id: 'auto-arrange',
      label: 'Auto-arrange',
      sublabel: 'Snap all cards to grid',
      group: 'Actions',
      icon: '⊞',
      action: onAutoArrange,
    });

    items.push({
      id: 'fit-to-view',
      label: 'Fit to view',
      sublabel: 'Zoom to show all clients',
      group: 'Actions',
      icon: '⊡',
      action: onFitView,
    });

    // Toggle pin on currently selected client
    const selectedClient = nodes.find((n) => n.type === 'client' && n.selected);
    if (selectedClient) {
      const d = selectedClient.data as ClientNodeData;
      const isPinned = !!d.client.pinned;
      items.push({
        id: 'toggle-pin-selected',
        label: isPinned ? `Unpin "${d.client.name}"` : `Pin "${d.client.name}"`,
        sublabel: 'Toggle star on selected client',
        group: 'Actions',
        icon: isPinned ? '★' : '☆',
        action: () => actions.togglePin(selectedClient.id),
      });
    }

    // Per-client items: jump + pin
    nodes
      .filter((n) => n.type === 'client')
      .forEach((n) => {
        const d = n.data as ClientNodeData;
        items.push({
          id: `jump-client-${n.id}`,
          label: d.client.name,
          sublabel: 'Jump to client',
          group: 'Clients',
          icon: '◎',
          action: () => jumpToNode(n.id),
        });
        items.push({
          id: `pin-client-${n.id}`,
          label: `${d.client.pinned ? 'Unpin' : 'Pin'} ${d.client.name}`,
          sublabel: 'Toggle star',
          group: 'Clients',
          icon: d.client.pinned ? '★' : '☆',
          action: () => actions.togglePin(n.id),
        });
        // Accounts within this client
        d.client.accounts.forEach((acc) => {
          items.push({
            id: `jump-account-${acc.id}`,
            label: acc.account_number,
            sublabel: `${acc.utility} · ${d.client.name}`,
            group: 'Accounts',
            icon: '⚡',
            action: () => jumpToNode(n.id),
          });
        });
      });

    // Unclassified accounts
    nodes
      .filter((n) => n.type === 'unclassified')
      .forEach((n) => {
        const d = n.data as UnclassifiedNodeData;
        items.push({
          id: `jump-unclassified-${n.id}`,
          label: d.account.account_number,
          sublabel: `${d.account.utility} · Unassigned`,
          group: 'Accounts',
          icon: '⚡',
          action: () => jumpToNode(n.id),
        });
      });

    return items;
  }, [nodes, actions, onAddClient, onAutoArrange, onFitView, jumpToNode]);

  // ── Filter & rank ──────────────────────────────────────────────────────────

  const filtered = useMemo<CommandItem[]>(() => {
    if (!query.trim()) {
      // Show recents + key static actions when no query
      const recentIds = loadRecent();
      const recentItems = recentIds
        .map((id) => allCommands.find((c) => c.id === id))
        .filter((c): c is CommandItem => c !== undefined);

      const staticDefaults = allCommands.filter((c) =>
        ['add-client', 'auto-arrange', 'fit-to-view'].includes(c.id),
      );

      const seen = new Set<string>();
      const result: CommandItem[] = [];
      for (const c of [...recentItems, ...staticDefaults]) {
        if (!seen.has(c.id)) {
          seen.add(c.id);
          result.push(c);
        }
      }
      return result;
    }

    return allCommands
      .map((c) => ({
        item: c,
        score: Math.max(
          fuzzyScore(query, c.label),
          (c.sublabel ? fuzzyScore(query, c.sublabel) * 0.7 : 0),
        ),
      }))
      .filter(({ score }) => score > 0)
      .sort((a, b) => b.score - a.score)
      .map(({ item }) => item);
  }, [query, allCommands]);

  // Reset active index when results change
  useEffect(() => {
    setActiveIdx(0);
  }, [filtered.length, query]);

  // Scroll active item into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>('[data-active="true"]');
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIdx]);

  // ── Execute ────────────────────────────────────────────────────────────────

  const execute = useCallback(
    (cmd: CommandItem) => {
      saveRecent(cmd.id);
      cmd.action();
      setOpen(false);
      setQuery('');
    },
    [],
  );

  // ── Keyboard nav ───────────────────────────────────────────────────────────

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, filtered.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const cmd = filtered[activeIdx];
      if (cmd) execute(cmd);
    } else if (e.key === 'Escape') {
      setOpen(false);
    }
  }

  // ── Group headers ──────────────────────────────────────────────────────────

  // Collect group labels in order (deduplicated)
  const groups = useMemo(() => {
    const seen = new Set<string>();
    const result: string[] = [];
    for (const c of filtered) {
      if (!seen.has(c.group)) { seen.add(c.group); result.push(c.group); }
    }
    return result;
  }, [filtered]);

  // ── Render ─────────────────────────────────────────────────────────────────

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[9000] flex items-start justify-center pt-[18vh]"
      style={{ background: 'rgba(0,0,0,0.35)', backdropFilter: 'blur(2px)' }}
      onMouseDown={(e) => {
        // Close when clicking the backdrop (not the palette itself)
        if (e.target === e.currentTarget) setOpen(false);
      }}
    >
      <div
        className="w-[600px] max-h-[420px] flex flex-col overflow-hidden rounded-2xl border border-cream-border bg-white shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-2 border-b border-cream-border px-4">
          <span className="shrink-0 text-zinc-400 text-sm select-none">⌘</span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Search actions, clients, accounts…"
            className="flex-1 py-3.5 text-sm text-zinc-900 placeholder-zinc-400 outline-none bg-transparent"
            autoComplete="off"
            spellCheck={false}
          />
          {query && (
            <button
              type="button"
              className="shrink-0 text-xs text-zinc-400 hover:text-zinc-600 px-1"
              onClick={() => { setQuery(''); inputRef.current?.focus(); }}
            >
              ✕
            </button>
          )}
        </div>

        {/* Results */}
        <ul
          ref={listRef}
          className="overflow-y-auto flex-1"
          role="listbox"
        >
          {filtered.length === 0 && (
            <li className="px-4 py-8 text-center text-sm text-zinc-400">
              No results for &ldquo;{query}&rdquo;
            </li>
          )}

          {groups.map((group) => {
            const groupItems = filtered.filter((c) => c.group === group);

            return (
              <div key={group}>
                <div className="px-4 pt-3 pb-1 text-[10px] font-semibold uppercase tracking-widest text-zinc-400 select-none">
                  {!query.trim() && group === filtered[0]?.group ? 'Recent & Suggestions' : group}
                </div>
                {groupItems.map((cmd) => {
                  const idx = filtered.indexOf(cmd);
                  const isActive = idx === activeIdx;
                  return (
                    <li
                      key={cmd.id}
                      role="option"
                      aria-selected={isActive}
                      data-active={isActive ? 'true' : undefined}
                      className={[
                        'flex items-center gap-3 px-4 py-2.5 cursor-pointer select-none transition-colors',
                        isActive
                          ? 'bg-primary-50 text-primary-900'
                          : 'text-zinc-700 hover:bg-zinc-50',
                      ].join(' ')}
                      onMouseEnter={() => setActiveIdx(idx)}
                      onMouseDown={(e) => { e.preventDefault(); execute(cmd); }}
                    >
                      <span
                        className={[
                          'shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-sm border',
                          isActive
                            ? 'bg-primary-100 border-primary-200 text-primary-700'
                            : 'bg-zinc-50 border-zinc-200 text-zinc-500',
                        ].join(' ')}
                      >
                        {cmd.icon}
                      </span>
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium truncate">{cmd.label}</div>
                        {cmd.sublabel && (
                          <div className="text-xs text-zinc-400 truncate">{cmd.sublabel}</div>
                        )}
                      </div>
                      {isActive && (
                        <span className="shrink-0 text-[10px] text-zinc-400 border border-zinc-200 rounded px-1 py-0.5">
                          ↵
                        </span>
                      )}
                    </li>
                  );
                })}
              </div>
            );
          })}
        </ul>

        {/* Footer hint */}
        <div className="flex items-center gap-4 border-t border-cream-border px-4 py-2 text-[10px] text-zinc-400 select-none">
          <span><kbd className="font-mono">↑↓</kbd> navigate</span>
          <span><kbd className="font-mono">↵</kbd> select</span>
          <span><kbd className="font-mono">Esc</kbd> close</span>
          <span className="ml-auto">⌘K</span>
        </div>
      </div>
    </div>
  );
}
