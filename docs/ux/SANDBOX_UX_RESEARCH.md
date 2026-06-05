# Sandbox UX Research: Towards a Sublime Canvas

*Research conducted June 2026. Sources: React Flow docs, Atlassian Design System, Heptabase wiki, Figma help docs, tldraw/Steve Ruiz viewport model, Atlassian Pragmatic DnD spec. Claims marked with source citations were adversarially verified (2/3 vote minimum). Claims without citations are synthesized from product observation and code review.*

---

## 1. The Problem Solar Operator Is Solving

A community solar operator in Vermont manages 7 to 50 clients, each of whom participates in one or more utility programs across GMP, VEC, and WEC portals. Every quarter, the operator needs to know: which arrays are generating, which clients own them, and what the credit numbers are. Today that knowledge lives in a human consultant's head — Solar Operator is building the software that replaces it. The Sandbox is the operator's control room: the place where they connect a portal login to the right client, discover that an account was miscaptured under the wrong name, drag a login group to its correct home, and generally maintain the ground truth of the relationship between clients, credentials, and solar installations. The canvas model is defensible — the data genuinely has a graph-like quality, clients are at different stages of setup, and operators do sometimes want to see the whole portfolio at once. But the current implementation forces a metaphor mismatch: it gives operators a whiteboard when they're really doing filing-cabinet work. Most operators don't have spatial intuitions about their clients — they have alphabetical, status-based, and utility-based intuitions. The canvas earns its keep only when there's an active reorganization task (a login belongs to the wrong client, two clients need merging). The rest of the time it's overhead.

---

## 2. Direct Inspirations

### 2.1 Heptabase — Section Titles That Survive Zoom

**The pattern they nail:** Navigability at macro scale. When you zoom out to see 40 cards on a Heptabase whiteboard, individual card text shrinks below readability. But section titles — which you created post-placement by selecting related cards and pressing Cmd+G — remain visible at any zoom level. You navigate by landmark name, not by spatial memory. At 10% zoom you see your map of named zones; zoom in and the cards reappear.

**The specific interaction:** Select multiple cards on the whiteboard, right-click → "Create Section," or press Cmd/Ctrl+G. The section border appears immediately. The title field activates inline. At low zoom, only the section title is readable; card text shrinks to illegible. This is confirmed behavior per official Heptabase documentation, not speculative.

**What Solar Operator could steal:** At roughly 20 clients, the canvas becomes hard to navigate. A named Groups feature (canvas-layer only, not a data model entity) lets operators say "these six cards are VEC clients" and see that label at any zoom. The implementation is a display-layer annotation — no backend change required. Cmd+G on selected client cards creates a Group with an editable title, a thin colored border, and a title that renders at 14px minimum regardless of zoom level. This solves the "100 cards" problem without a minimap — the map *is* the canvas.

---

### 2.2 Atlassian Pragmatic DnD — Drop Indicators as a UX Contract

**The pattern they nail:** Drag-and-drop that communicates intent before the drop happens. Atlassian's design system specifies the exact values that make this feel production-grade rather than prototype-grade.

**The specific interaction:** When dragging a Jira issue toward a list, a 2px-stroke indicator line with an 8px terminal dot appears at the precise insertion point. The transition animation uses cubic-bezier(0.15, 1.0, 0.3, 1.0) at 350ms — fast enough to feel responsive, slow enough to not feel janky. The origin item stays in place at 40% opacity during the drag, giving the user a spatial anchor: they can still see where the item came from. For collapsed tree nodes, the target auto-expands after 500ms of hover. These are not guesses — they are published, production-tested values from Atlassian's design system documentation.

**What Solar Operator could steal:** The login row drag currently provides no drop affordance on the destination card beyond a cursor change. Applying the Atlassian spec: when a login row is being dragged and the pointer enters a client card, the card should show a 2px ring (in the utility's color — emerald for GMP, blue for VEC, amber for WEC) and a ghost login row preview appearing as the last item in that card's expanded login list. If the destination card is collapsed, auto-expand it after 400ms hover. The origin login row fades to 40% opacity during the drag so the user retains spatial context.

---

### 2.3 Figma — Selection Model for Nested Objects

**The pattern they nail:** A click model that protects users from accidental mutations while still making intentional deep selection fast.

**The specific interaction:** Clicking a nested object in Figma selects the parent frame or group by default. Double-click descends one nesting level. Press Enter to descend further. This parent-first model means a casual click on a frame never accidentally selects or moves a child component. For bulk operations, Figma allows multi-select across different container types simultaneously — you can shift-click objects inside different frames and they all join the selection. Figma's documented "Multi-edit text" mode applies text changes to the entire cross-container selection at once.

**What Solar Operator could steal:** The multi-select model (Shift already works via React Flow's `multiSelectionKeyCode="Shift"`) but the visual feedback is weak. When two or more client cards are selected, the toolbar should surface bulk actions prominently: "Delete 3 selected," "Move to group," "Export report for 3 clients." More importantly: when a login row drag starts, the system should detect if multiple client cards are selected and offer to move the login group to all selected clients (rare but valid for same-operator-multiple-clients scenarios).

---

### 2.4 Notion — Drag Handles That Teach Without Onboarding

**The pattern they nail:** Making drag affordances discoverable without a tutorial.

**The specific interaction:** Hover any Notion block and a six-dot icon (⣿) appears to the left of the content, offset from the text so it doesn't interfere with selection. It appears within roughly 150ms of hover. Clicking the icon opens a block action menu. Dragging the icon moves the block. The handle is always the same icon, always in the same position, never ambiguous. A thin blue insertion line appears during drag to show the landing point.

**What Solar Operator could steal:** The login rows in `LoginGroupRow` are draggable via the entire `div`, but there is no visual indicator that they're draggable until you happen to hover and read the `title` tooltip. Adding a six-dot icon to the left margin of each login row — outside the click-to-expand target — immediately communicates draggability without a walkthrough step. The icon should change to a grab cursor (`cursor-grab`) on hover and `cursor-grabbing` during drag. This costs one SVG and two CSS lines.

---

### 2.5 tldraw / Excalidraw — Zoom-to-Cursor That Never Fights You

**The pattern they nail:** Viewport model that makes zoom feel physically grounded.

**The specific interaction:** In tldraw and Excalidraw, scroll-to-zoom anchors to the pointer position — the canvas coordinate under the cursor stays under the cursor as zoom level changes. Steve Ruiz's canonical post on zoom UIs explains the underlying model: the camera position is the **top-left corner of the viewport**, not the center. The viewport extends down and right from the camera, not outward from a midpoint. This detail is critical for zoom-to-cursor math; a center-anchored model pushes content toward screen edges on zoom-in, which feels like the canvas is fighting you.

**What Solar Operator could steal:** React Flow handles this correctly out of the box, which is a strength. The lesson is defensive: any future custom viewport logic (e.g., programmatic zoom-to-fit-selection, center-on-new-client animations) must use React Flow's `setCenter` and `fitView` APIs rather than manual camera math, or the zoom behavior will break. The current `centerOnClientId` already uses `setCenter` — preserve this pattern.

---

### 2.6 Mercury — Density-Aware Tool Surfaces

**The pattern they nail:** Showing only the tools that matter for the current data volume.

**The specific interaction:** Mercury's transaction list adapts to density. At zero transactions, there is a welcoming empty state. At one transaction, the empty state is gone and the transaction is the star. At 50 transactions, a search bar and filter chips appear that weren't present at five transactions. The product reads the data density and promotes tools proportionally. There is no moment where a user with one transaction is shown a complex filter UI they can't use yet.

**What Solar Operator could steal:** The toolbar today renders identically at 1 client and at 50 clients. The density controls, auto-arrange button, and bulk delete only make sense at 8+ clients. A density-aware toolbar would: (a) hide density controls until ≥6 clients; (b) hide the minimap button until ≥15 clients; (c) replace the "Fit to view" button with an empty-state CTA when there are 0 clients; (d) show an "Add first client" spotlight when there's exactly 1 client and the walkthrough is active.

---

### 2.7 Linear — Command Palette as the Operator's Address Bar

**The pattern they nail:** Surfacing recent context without making the user remember.

**The specific interaction:** Linear's Cmd+K opens with the five most recently touched issues pre-loaded — no query required. Typing searches everything: issue IDs, titles, team names, labels. Each result shows a sublabel with full context ("ENG-423 · Backlog · No assignee") so the user can disambiguate without reading the full title. Keyboard navigation is instant. Hitting Enter jumps the view to that item without requiring the user to remember where it lives in the workspace.

**What Solar Operator could steal:** The Cmd+K palette in Solar Operator already implements recents and fuzzy search — that's ahead of most tools at this stage. The gaps: (a) sublabels for client entries say "Jump to client" when they should say "GMP · VEC · 3 arrays" — the context that helps operators distinguish "Green Mountain Co-op" from "Green Mountain Homes"; (b) the palette should include filter actions at the top level — "Sort by name," "Show pinned only," "Group by utility" — so operators who've forgotten the toolbar can find these actions by typing; (c) search for account numbers (already implemented) but also for array names and NEPOOL IDs.

---

### 2.8 Miro — Frames as the Scalability Ladder

**The pattern they nail:** Managing a board that grows from 10 to 200 nodes without restructuring.

**The specific interaction:** Miro frames are resizable containers with sticky titles. At normal zoom, the frame is labeled and its contents are fully visible. As you zoom out, frame titles remain fixed-size while content shrinks. Frames appear in the sidebar as navigation items — "Planning Sprint," "Client X Review" — so you can jump by name. The board becomes a map of zones, not an undifferentiated field of shapes.

**What Solar Operator could steal:** The Groups concept from the Heptabase section above, combined with Miro's sidebar navigation. A Groups panel in the bottom-left (collapsible) lists all named groups, shows a client count per group, and lets you jump to any group with one click. At 50+ clients, this is the difference between "I need to scroll around to find the VEC clients" and "I click 'Vermont Electric Coop clients' in the sidebar and the canvas pans there."

---

## 3. The Five Biggest UX Wounds

### Wound 1: Free Positioning Optimizes for a Preference Operators Don't Have

The 2D canvas assumes operators care where their clients live spatially — northwest vs. southeast, clustered vs. scattered. They don't. Operators care about alphabetical order, recency of capture, completion status, and utility type. The current `autoArrange()` function resets to a static 4-column grid — useful for initial setup but without any sort key, it's just randomness with straight lines. The result: every operator builds a personal spatial memory ("Starlight Solar is somewhere in the top-left quadrant") that decays over weeks and becomes useless at 30 clients. Compare this to Airtable, which defaults to a sorted list and requires an intentional step to enter the gallery/spatial view.

The canvas is the right *occasional* interaction model — when you're dragging a login to a different client, spatial context matters. But it should not be the default *browsing* model. The auto-arrange should support sort keys (alphabetical, last captured, array count) and run the sort on every load unless the user has explicitly enabled "free positioning" mode.

### Wound 2: The Login Row's Drag Affordance Is Hidden Until You Know It Exists

The `LoginGroupRow` component is draggable, but the only signal is a `title` attribute: "Drag to move all accounts under this login to another client." That tooltip appears after a 1-second browser hover delay, only on mouse rest, only if the user holds the cursor still enough to not trigger the expand click. There is no grab icon, no cursor change on hover, no visual distinction between the login row (draggable) and the array items inside it (not draggable). New operators will never discover the drag workflow from inspection — they'll stumble onto it by accident, if at all. This is the single most important interaction in the product (it's how wrong-client captures get corrected) and it has no affordance.

### Wound 3: Density Transition Silently Stomps User Positions

`deriveDensity()` switches from `full` to `compact` at 6 clients. This density change, when triggered by count threshold (not user action), does *not* call `autoArrange()` — that's intentional. But when the user then manually clicks "Auto" or "Compact" in the density control, `handleDensityChange` sets `userDensityActionRef.current = true` and the next `useEffect` fires `autoArrange()`, which reassigns all card positions. Operators who've carefully arranged their 6-card canvas will have their layout nuked the first time they touch the density control. Worse, the density control is one of the first toolbar items from the right — operators will naturally experiment with it.

The root fix: density should control card *render size* only, never trigger a position reset. Auto-arrange should be a completely separate, explicit user action, not a side effect of density changes. A user changing from "Full" to "Compact" should see their cards shrink in place, not fly to new grid positions.

### Wound 4: Drag-to-Merge Is Irreversible but Uses a Low-Friction Gesture

The merge interaction (`onNodeDragStop` detects client overlap, fires `setMergeDialog`) triggers from a gesture — dragging one card onto another — that happens accidentally on any crowded canvas. The merge dialog itself is understated: two soft buttons with no warning about permanence. And the undo is a UI-only illusion: the comment in `confirmMerge` says verbatim "NOTE: server state is still merged — this is a view-only revert." An operator who accidentally merges two clients, hits Cmd+Z, sees the cards separate, then reloads — finds the merge persisted.

Merge is a permanent, destructive action masquerading as a spatial rearrangement. At minimum, the dialog should say "This cannot be fully undone — the surviving client will permanently absorb all accounts from the other." Better: remove drag-to-merge entirely and put it in the right-click context menu and Cmd+K ("Merge into..."), where the interaction cost matches the action cost.

### Wound 5: The Walkthrough Graduates Users Before They've Encountered Hard Problems

`initStep()` returns `'done'` at `clientCount >= 3`. Three clients. At three clients, the operator has not yet experienced: density compression (kicks in at 6), a misassigned login (discovered during month 2), the auto-arrange nuclear reset, a merge (rare but catastrophic if accidental), or the Cmd+K palette (power feature, never surfaced in walkthrough). The walkthrough teaches "here is a card, here is the Add button" — valuable for the first 10 minutes — but it ends before any of the actually confusing interactions are encountered.

A better model: the walkthrough is event-driven, not count-driven. It fires a new tip the first time the user encounters density compression ("Your cards just got smaller — that's Auto density. You can lock it with this control"), the first time a login is unclassified ("This account didn't land in a client — drag it to assign it"), and the first time two cards overlap during a drag ("Dropping a card onto another client will merge them — are you sure?"). These moments are teachable precisely *because* they're confusing in context.

---

## 4. The Sublime Reimagined Sandbox (North Star, 6–12 months)

### Default Layout: Sorted Grid, Free Canvas as Opt-In

The canvas opens in "Sorted" mode: a 4-column grid, alphabetical by default, with a sort key picker in the toolbar (name, last captured, array count, completion status). Cards are at fixed positions computed from their sort rank — no free positioning. A button in the toolbar reads "Arrange freely" and, when clicked, unlocks drag-to-position for the current session. The sorted grid re-applies on page load unless the user has explicitly saved a manual layout.

This is Airtable's philosophy applied to a canvas: the structured view is default, the spatial view is opt-in, and the two coexist without fighting. Operators who want to cluster their GMP clients together can do so; operators who just want to find "Smith Farm" fast get it at position A-S in the grid.

### Groups Emerge from Selection

Multi-select any set of client cards (Shift+click or drag-select) and press Cmd+G. A group appears: a thin border (2px, rounded corners, the primary color at 20% opacity), an editable title above it ("VEC Clients," "2025 Q3 Onboarding," "Bruce's Accounts"), and a collapse arrow. When collapsed, the group shows as a single row — the title plus a client count badge. When the canvas is zoomed out below 30%, group titles render at minimum 12px regardless of zoom level. Card internals shrink; group borders and titles remain readable.

Groups are canvas-only — no data model change, no API call. They persist in localStorage keyed to the user. Deleting a group removes the border and title; the cards remain.

### A Brand-New Login Enters the Canvas with a Moment

When the Chrome extension fires the `so:capture-cleared` event, the canvas: (1) silently reloads in the background, (2) auto-pans to the new client card with a 600ms easing animation, (3) plays a brief scale pulse (1.0 → 1.04 → 1.0 at 120ms) on the new card, (4) auto-expands the card so the login row is visible, (5) renders the login row with a six-dot drag handle glowing for 3 seconds ("drag this if it belongs to a different client"). The walkthrough callout — currently just a static arrow — becomes a one-time animated spotlight that the operator can dismiss by clicking anywhere.

The gesture teaches itself: the glow on the login row handle invites a drag before the operator has even read the tooltip.

### 50 Clients Feels Like a Map

**At 1–5 clients:** Canvas is clean. Toolbar shows Add Client and Undo only. No density control, no minimap — they're noise at this scale.

**At 6–15 clients:** Density control appears. Cards shift to Compact automatically but *positions are preserved*. A "Groups" button appears in the toolbar (inactive until first group is created).

**At 16–40 clients:** MiniMap panel activates in the bottom-right (React Flow's `MiniMap` component, 160×100px, tinted to match the background). Group titles are visible on the minimap as colored zones. The sort key picker is prominent.

**At 40–50 clients:** Auto-suggest grouping: "You have 12 GMP clients — create a group?" as a dismissable banner below the toolbar.

**At 50+ clients:** A left sidebar ("Groups") lists all named groups with client counts. Clicking a group name pans and zooms to it. The canvas background shifts from a dot grid to a subtle zone grid that aligns with group boundaries.

### Drag/Drop Targets: One Consistent Metaphor

Every draggable element has a six-dot icon (⣿) in its left margin. The icon uses `cursor: grab` on hover and `cursor: grabbing` during drag. Every valid drop target shows a 2px ring in the appropriate color when a compatible drag is in progress. Login rows dragged over a client card show a preview of that row appearing inside the target. Cards dragged onto other cards do *not* trigger merge from drag overlap — merge is right-click and Cmd+K only. The 40% origin opacity (Atlassian spec) applies to all drag operations.

### Color, Motion, and Sound

Utility colors (GMP emerald, VEC blue, WEC amber) extend to: group border colors when a group contains only one utility type, MiniMap zone tints, and drop indicator rings during login drags. Motion: new cards pop-in with a 120ms scale ease-out; deleted cards exit with a 200ms scale-down + fade; successful drops confirm with a 150ms "settle" animation (the destination card pulses its border color once). Sound: opt-in via a "Sound feedback" toggle in settings (default off). When enabled: a 40ms, 440Hz sine wave at −22dB on successful drop; a lower 220Hz tone on cancel. These are not decorations — they create a physical sense of cause and effect that reduces the need to look at toast messages after every operation.

### What the Operator Never Has to Think About

- Where to put a new card (sorted grid handles it; free mode is available but not the default)
- Whether a login row is draggable (the handle is always visible on hover)
- Whether a merge was accidental (merge is no longer a drag gesture)
- Which clients need recapture (completion badges — green ring for current, amber for due soon, red for stale — are visible at all density levels)
- Where "Johnson Farms" is on the canvas (Cmd+K finds it instantly; sorted grid puts it at J)

---

## 5. Concrete Next 10 Improvements (Prioritized by Impact / Effort)

### 1. Six-dot drag handle on login rows — **S effort, Critical impact**
Add a `⣿` icon to the left margin of `LoginGroupRow`, styled with `cursor: grab` on hover. Use it as the drag initiation point while keeping the rest of the row as the click-to-expand target. This is the single highest-ROI change in this list: it makes the primary reorganization gesture discoverable without any walkthrough changes. One SVG, two CSS properties.

*Preserves current canvas model.*

### 2. Decouple density change from auto-arrange — **S effort, High impact**
Remove the `userDensityActionRef` + `autoArrange` side effect from `handleDensityChange`. Density changes should rerender card sizes only; positions should be untouched. Auto-arrange stays as an explicit toolbar button. This prevents the "I touched the density control and lost my layout" experience that will hit every operator around day 3.

*Preserves current canvas model.*

### 3. Drop indicator ring on login drag target — **S effort, High impact**
When `e.dataTransfer.types` includes `application/x-so-login`, and the pointer is over a client card, render a 2px colored ring (in the dragged login's utility color) around the card. Show a ghost row preview inside the card's expanded login list area. On drop, remove the ring and confirm with a 150ms border pulse. Uses the existing `dropHover` state in `ClientNode` — extend it to carry the utility color.

*Preserves current canvas model.*

### 4. Sort key for auto-arrange — **S effort, Medium-high impact**
Add a sort key picker next to the Auto-arrange button: `name ↑`, `last captured`, `arrays: most`, `completion`. When the user clicks Auto-arrange, sort by the selected key before computing grid positions. Default: alphabetical by name. This turns the "nuclear option" of auto-arrange into something an operator would willingly use weekly.

*Preserves current canvas model.*

### 5. Move drag-to-merge to context menu only — **S-M effort, High safety impact**
Remove the `setMergeDialog` trigger from `onNodeDragStop`. Add "Merge into..." as a context menu item on client cards — clicking it opens a "select which client to merge into" flow, same dialog, but triggered intentionally. Add a "Merge" option to Cmd+K palette as well. The accidental-merge failure mode (permanent data loss) is eliminated; the workflow remains available.

*Rethinks a specific interaction; preserves overall canvas model.*

### 6. Client completion badges — **M effort, High impact**
Add a status ring to client card avatars: green (has arrays + capture < 35 days old), amber (has arrays + capture 35–90 days old), red (has arrays + capture > 90 days old or missing), grey (no arrays yet). The ring is 2px, outside the avatar circle, visible at all density levels including `dense`. This gives operators a weekly "recapture sweep" workflow: open canvas, find red rings, recapture those clients. No separate reporting dashboard needed.

*Requires new API field or client-side computation from last-captured date.*

### 7. React Flow MiniMap — **S effort, Medium impact at scale**
Add `<MiniMap />` from `@xyflow/react` to `SandboxCanvas`, hidden until `clientCount >= 15`. Position: bottom-left, 160×100px, borderRadius 8px, matching the canvas background tone. Node colors in the minimap should reflect utility chip colors (GMP green, VEC blue, mixed neutral). This is a one-component addition that solves the "where am I on the canvas" problem for 20+ client deployments.

*Preserves current canvas model; additive only.*

### 8. Sublabel context in Cmd+K client entries — **S effort, Medium impact**
Change `sublabel: 'Jump to client'` in `CommandPalette` to `sublabel: \`\${chips.join(' · ')} · \${arrayCount} arrays\`` — where chips are the utility types this client has. "GMP · VEC · 4 arrays" tells the operator far more than "Jump to client" when disambiguating between similarly-named entries.

*Preserves current canvas model.*

### 9. Zoom-level card simplification — **M effort, Medium-high impact**
Below 40% zoom, transition client cards from full render to a simplified "name + utility dot row" view. At 20% zoom, show only the client name and colored dots (one per utility type). This requires detecting the current zoom level (available from React Flow's `useReactFlow()` `getZoom()`) and passing it as a prop or context to `ClientNodeComponent`. The transition should be a CSS opacity fade, not a snap. This is the Heptabase section-title pattern applied at the card level.

*Preserves current canvas model; adds a new rendering tier.*

### 10. Event-driven walkthrough tips — **M effort, High onboarding impact**
Instead of exiting the walkthrough at 3 clients, trigger contextual tips for the interactions that actually confuse operators. Three new events: (a) first time `density` transitions automatically → tip "Your cards got smaller — that's Auto density. Lock it here." (b) first time an unclassified account node appears on canvas → tip "This account didn't land in a client — drag it to assign it." (c) first time `mergeDialog` is about to fire → intercept with a warning "Merging is permanent — are you sure?" The existing walkthrough's localStorage key system already supports this pattern.

*Rethinks the walkthrough trigger model; preserves visual overlay approach.*

---

## 6. Open Questions for Ford

**Q1: Free canvas vs. sorted grid as default — which is the real workflow?**
This is the strategic question behind Wounds 1 and 3. If operators are primarily *discovering and correcting* data (drag login X to correct client Y), then the spatial canvas is the right default. If they're primarily *browsing and monitoring* (find client X, check its status, recapture if needed), then a sorted grid with the canvas as a detail view is better. The answer changes the architecture of the next 12 months significantly. Worth testing with Bruce: does he navigate the canvas spatially, or does he use Cmd+K / manual scanning?

**Q2: What is the right "onboarding complete" signal?**
The current signal (3 clients) is too early. Options: (a) count-based but higher (8+ clients), (b) interaction-based (first successful login drag), (c) task-based (first quarterly report generated), (d) time-based (7 days after first login). Each implies a different theory of what "learned the product" means. The research found no consensus on this from the products surveyed — it's a product decision about what you want operators to internalize before you stop holding their hand.

**Q3: Should groups/sections exist in the data model?**
Canvas-only groups (localStorage) are free to build but disappear if the operator logs in on a different machine. Data-model groups (new DB table, API endpoint) persist everywhere but require a migration and add schema complexity. The question is whether Solar Operator operators will log in from multiple devices, or whether the primary workflow is always one machine. If it's one machine (likely for a solo consultant like Bruce), localStorage is fine for now and the data model can be deferred.

**Q4: Is drag-to-merge worth keeping at all?**
The current drag-to-merge has broken undo semantics (UI-only revert, server state persists), is triggered by accidental overlap, and creates permanent data mutations. The question is whether the gesture has any user value that a right-click "Merge into..." cannot provide. If no operator has intentionally used drag-to-merge without being surprised by it, remove it entirely. If operators actively use it and know what they're doing, keep it but fix the undo path to actually un-merge on the server.

**Q5: At what client count does Solar Operator become a multi-operator tool?**
Everything in this document assumes one operator managing one portfolio. If Solar Operator grows to teams — two consultants splitting 60 clients — the canvas needs collaborative presence (who's editing what), conflict resolution, and permission scoping. The current canvas has no notion of concurrency. This doesn't need to be solved now, but the architectural question of "will groups/assignments be per-operator or shared?" needs an answer before any group data model work begins.

---

*Sources: [React Flow sub-flows docs](https://reactflow.dev/learn/layouting/sub-flows), [Atlassian Pragmatic DnD design guidelines](https://atlassian.design/components/pragmatic-drag-and-drop/design-guidelines), [Heptabase organize knowledge wiki](https://wiki.heptabase.com/organize-knowledge-and-projects), [Figma select layers docs](https://help.figma.com/hc/en-us/articles/360040449873-Select-layers-and-objects), [Figma bulk edit docs](https://help.figma.com/hc/en-us/articles/21635177948567-Edit-objects-on-the-canvas-in-bulk), [tldraw zoom-ui model by Steve Ruiz](https://www.steveruiz.me/posts/zoom-ui), [Heptabase keyboard shortcuts](https://wiki.heptabase.com/keyboard-shortcuts)*
