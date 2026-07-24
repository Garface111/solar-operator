/** THE roster rule — which clients an operator is shown.
 *
 * Inside the Array Operator "Generation reports" embed we hide retired
 * (inactive) clients: a folded tenant can carry dozens of inactive
 * capture-artifact clients that aren't part of its reporting world (Ford's own
 * tenant: 2 active, 96 retired). The standalone /accounts SPA leaves the flag
 * unset and still shows them, because its reactivate flow lives on those rows.
 *
 * This lives in ONE place because it was applied in one surface and not the
 * other: the Clients table filtered, the sandbox canvas did not, so a retired
 * client ("Bruce Genereaux", 41 arrays) rendered a card in the sandbox that had
 * no row in the table (Ford, 2026-07-24). Every surface that paints a client
 * roster must call this — never re-derive the rule inline. Same doctrine as
 * api/report_arrays.py on the backend.
 *
 * Render-only: callers keep their raw list intact so mutation/merge/undo logic
 * is unchanged.
 *
 * ⚠️ Evaluated at RENDER, never at module load. embed.tsx sets
 * window.__soGenrepEmbed in its own module body, which the bundler runs AFTER
 * an imported module's top level — a module-level const captures `false` and
 * the filter silently never fires (this exact bug shipped once already).
 */
export function hideInactiveClients(): boolean {
  return (
    typeof window !== "undefined" &&
    (window as { __soGenrepEmbed?: boolean }).__soGenrepEmbed === true
  );
}

/** Apply the roster rule to any client-ish list. `active` absent ⇒ treated as
 *  active, so a payload that predates the field is never blanked out. */
export function rosterClients<T extends { active?: boolean }>(clients: T[]): T[];
export function rosterClients<T extends { active?: boolean }>(
  clients: T[] | null,
): T[] | null;
export function rosterClients<T extends { active?: boolean }>(
  clients: T[] | null,
): T[] | null {
  if (!clients || !hideInactiveClients()) return clients;
  return clients.filter((c) => c.active !== false);
}
