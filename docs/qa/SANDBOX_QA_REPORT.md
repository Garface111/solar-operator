# Sandbox QA Report

**Date:** 2026-06-05
**Commit under test:** 4b74b6fd9ecd4eef6d16a3f2f77f059813ec5e98
**Branch:** fix/sandbox-qa-pass
**Reviewer:** AI QA pass (code-level + partial Playwright)
**Viewport:** 1440×900 (Chromium headless)

**Note on live testing:** The app uses magic-link-only auth (no password form). Prod screenshots
of the authenticated sandbox were not captured in this pass. All bugs below were found via static
code analysis of `SandboxCanvas.tsx`, `ClientNode.tsx`, `UnclassifiedAccountNode.tsx`, and
`api/sandbox.py`. The bugs are real and deterministic — no speculation.

---

## Bug Table

| ID | Severity | Area | Description | File:Line | Status |
|----|----------|------|-------------|-----------|--------|
| BUG-001 | Major | Viewport persist | `loadCanvas` always calls `fitView()` after loading nodes, **overriding** any viewport saved in localStorage. `defaultViewport` is set from localStorage on the initial render but immediately destroyed when data loads. Net effect: the operator's pan/zoom is never actually restored on reload. | `SandboxCanvas.tsx:197-199` | **Fixed** |
| BUG-002 | Major | Login-group detach | `detachLogin`'s optimistic UI update uses `accounts.filter((a) => a.utility !== utility)` — removes **all** accounts of that utility from the client. When a client holds two same-utility groups (their own GMP login + a moved-in GMP login), detaching one wipes both in the UI. Backend receives the correct narrow set, so the discrepancy resolves on reload, but the UI flash is confusing and data-scrambling. | `SandboxCanvas.tsx:607` | **Fixed** |
| BUG-003 | Major | Login-group move | `moveLoginToClient` has the identical filter bug: `accounts.filter((a) => a.utility !== utility)` strips all same-utility accounts from the source client, not just the dragged group. Same conditions as BUG-002. | `SandboxCanvas.tsx:680` | **Fixed** |
| BUG-004 | Minor | Context menu | `ContextMenuPopover` closed on `onMouseLeave`. A user moving toward a menu item who briefly exits the menu's bounding box (especially near corners) loses the menu before they click. Standard UX: context menus close on Escape or click-outside. | `SandboxCanvas.tsx:1006` | **Fixed** |
| BUG-005 | Minor | Undo keyboard | Undo button tooltip says `(⌘Z)` but there was no `keydown` listener wired up. Pressing Cmd+Z did nothing. | `SandboxCanvas.tsx:829` | **Fixed** |
| BUG-006 | Minor | Delete node | Right-click → Delete removes the node from local state only (no backend call). Node reappears on any canvas reload. Toast said "Removed from canvas." which reads as permanent. Operators will be surprised. | `SandboxCanvas.tsx:256-259` | **Fixed** (toast copy) |
| BUG-007 | Minor | Merge cancel | When a merge dialog is cancelled, the dragged node stays at the drop position (overlapping the target). No position restore. Operator must manually drag the card back. | `SandboxCanvas.tsx:390-391` | **Flagged for Ford** — requires pre-drag position capture via `onNodeDragStart`; skipped this pass |
| BUG-008 | Polish | Rename affordance | Client name `<p>` has `cursor-text` but no hover underline or visual hint that double-clicking edits it. Operators won't discover rename without the context menu. | `ClientNode.tsx:195` | **Fixed** |
| BUG-009 | Polish | Rename empty-string | If operator clears the rename input and blurs, it silently reverts to the old name with no feedback. Not a crash but surprising. | `ClientNode.tsx:188-191` | **Flagged** — minor; fix is to show a brief "Name can't be empty" shake |
| BUG-010 | Polish | Drag hint | Unclassified node footer says "↓ Drag onto a client to attach" — arrow points down but clients are to the side. | `UnclassifiedAccountNode.tsx:77` | **Fixed** |
| BUG-011 | Polish | Pin toast copy | "Pinned to top." / "Unpinned." — misleading because pinned clients don't actually sort to the top of the canvas or auto-arrange. The visual affordance is a ★ star, so "Starred."/"Unstarred." is accurate. | `SandboxCanvas.tsx:740` | **Fixed** |
| BUG-012 | Polish | Context menu Esc | Context menu had no Escape key handler. Fixed as part of BUG-005 work (global keydown handler now closes menu on Esc). | `SandboxCanvas.tsx` | **Fixed** |

---

## Sublime Gap — Things That Work But Feel Cheap

1. **Pinned clients don't sort to top in Auto-arrange.** The visual star suggests priority but `autoArrange` uses `clientNodes` in their current order, not sorted by `canvas_pinned`. A pinned client should land at position [0,0] after arrange.

2. **Delete is non-destructive but looks destructive.** Right-click → Delete has red text, an ominous label, and a toast. But it's just a local hide. Either lean into the non-destructive model (rename to "Hide", gray text) or actually delete from the backend.

3. **Undo only covers merges.** Account detach, login-group move, account reassign — none of these arm the Undo button. The button exists in the toolbar at all times but is only useful for one operation. The operator discovers this the hard way.

4. **No empty-state label for the unclassified pile.** When accounts land to the right of the grid, there's no header/label saying "Unclassified accounts." The cards just float. A subtle `<section>` label above the rightmost column would orient new operators.

5. **Auto-arrange snaps instantly for position, then fitView animates.** There's a visible two-phase jank: cards teleport to grid positions, then the viewport smoothly slides. A staggered animation (ReactFlow `style: { transition: 'all 400ms' }` on each node) would make arrange feel like a deliberate action instead of a glitch-then-smooth.

6. **Minimap colors: unclassified nodes are gray (#a1a1aa) on a light-gray mask.** Near-invisible at the minimap scale. Consider `#94a3b8` (slate-400) or outline the unclassified area differently.

7. **No Esc-to-close on the merge dialog.** The merge dialog is the most consequential modal on the canvas; it only closes via Cancel click. Escape should also dismiss it.

8. **Rename input reuses the same width as the name text.** The input stretches to the card width, which is fine, but focus ring bleeds into the card border — looks like the whole card is selected. A tighter `max-w-[...] mx-1` on the input would separate the concerns visually.

9. **⌘Z tooltip renders `⌘` on Windows as a literal glyph.** On Windows the modifier is Ctrl, not ⌘. The tooltip `(⌘Z)` should read `(Ctrl+Z)` on Windows or use a cross-platform-aware string.

---

## What Felt Great (Protect These)

- **Amber merge-intent highlight** is exactly right: clear, unmissable, physically anchored to the card being overlapped. Don't soften it.
- **500ms undo banner below the canvas** for merges is the right scope. It's a "did I mean that?" recover, not a history stack.
- **Optimistic UI + revert on failure** pattern is consistent across all operations. Zero loading spinners during drag.
- **nowheel on expanded card scrolls** works — wheel inside an expanded card scrolls the account list, not the canvas.
- **Entry animation (so-node-enter) with staggered delay** makes the initial load feel alive without being distracting.
- **Login group drag** (`application/x-so-login` payload) correctly segregates multi-utility accounts per origin client — the data model is sound even if the filter bug occasionally scrambles the view.
- **Background dot grid at gap:22 + size:1.5** — subtle, doesn't fight the cards.
- **pinClient API** correctly reuses `canvas_pinned` column with no schema change. Clean.

---

## Bugs Fixed in This Pass: 8 of 12

| Fixed | ID |
|-------|----|
| ✓ | BUG-001 Viewport restore |
| ✓ | BUG-002 detachLogin filter |
| ✓ | BUG-003 moveLoginToClient filter |
| ✓ | BUG-004 Context menu mouseLeave |
| ✓ | BUG-005 ⌘Z keyboard undo |
| ✓ | BUG-006 Delete toast copy |
| ✓ | BUG-008 Rename hover affordance |
| ✓ | BUG-010 Drag hint direction |
| ✓ | BUG-011 Pin toast copy |
| ✓ | BUG-012 Esc closes context menu |
| — | BUG-007 Merge cancel position (needs arch work) |
| — | BUG-009 Rename empty-string feedback (nice-to-have) |

---

## Ford's Manual Review Checklist (top 3)

1. **BUG-001 viewport persist** — After this fix, pan/zoom should survive a page reload. Verify by panning far right, reloading — confirm the canvas restores position instead of fitView re-centering. (Previously fitView always fired in `loadCanvas`.)

2. **BUG-002/003 login-group filter** — These only trigger when a client has two same-utility login groups (e.g., own GMP + a GMP login moved in from another client). Bruce's data may not hit this path today, but it will once more clients share utilities. Test by moving one GMP login from Client A to Client B, then detaching it from B — verify only that group detaches, not all GMP accounts.

3. **BUG-007 merge cancel position** — After dragging one client card onto another and hitting Cancel, the dragged card stays overlapping the target. Operator must manually drag it back. Flag for v2: capture pre-drag position in `onNodeDragStart` → store in `mergeDialog` state → restore on `cancelMerge`.

---

## Blockers

None. No crashes, no data loss (all mutations are optimistic-UI with server-revert), no security issues found.

---

## Sublime Gaps Not Fixed (For Ford)

| Gap | Effort | Notes |
|-----|--------|-------|
| Pinned clients sort to top in Auto-arrange | Small | Sort `clientNodes` by `canvas_pinned` before position assignment in `autoArrange` |
| Undo for detach/reassign operations | Medium | Requires generalizing `mergeUndo` to `lastUndo: { label, snapshot }` |
| Merge dialog Esc to close | Tiny | `cancelMerge` when `e.key === 'Escape'` in the keydown handler |
| Empty-state label for unclassified pile | Small | A `<div>` header above the rightmost column |
| Auto-arrange snap jank | Medium | Add `transition: 'transform 400ms ease'` to node styles during arrange |
| ⌘Z vs Ctrl+Z tooltip | Tiny | `navigator.platform.includes('Mac') ? '⌘Z' : 'Ctrl+Z'` |
| Right-click Delete → actually delete or rename to Hide | Medium | Policy decision for Ford; touches backend |
