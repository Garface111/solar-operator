# The Rocket Engine — Sovereign as contained voracity

**Ford vision (2026-07-22).** Not a ticket bot. Not an axiom printer.  
A **pressure chamber**: wild mind + hard walls + one exit (product better, not broken).

## Why previous attempts failed

| What we did | What happened |
|-------------|----------------|
| “Self-improve” | Model wrote **axioms and tools lists** — looks like progress, isn’t |
| “Sandbox” | Model still saw **prod URLs**, desk, real queues — knew it was fake OR still touched real ops |
| Free run | **No weekly 1:1 comparison** of site A vs site B as the score |
| Guardrails only | Killed energy without giving a **direction of thrust** |

The model will always collapse vague freedom into **safe bureaucracy** unless we force:

1. **False-real chamber** (it believes this *is* the product)  
2. **Oxidizer** (language that demands voracity, curiosity, vacuum-filling)  
3. **Nozzle** (only outlet = measurable product delta, never axiom spam)  
4. **Housing** (infra that survives thrash without AO going down)  
5. **Score** (you compare chamber vs prod on a clock)

## Metaphor → engineering

| Metaphor | Engineering |
|----------|-------------|
| Fuel | LLM (Grok primary) + continuous ticks |
| Oxidizer | **Drive prism** — psychotic/curious pressure prompt + anti-stagnation penalties |
| Combustion chamber | **False-real sandbox** — identical AO surface, not production |
| Engine bell / nozzle | **Only exit**: ship visible product improvement *inside the chamber* |
| Walls / manifold | Pool guards, single-flight, no main push, rate limits, tests |
| Telemetry | Diffs, pitch, weekly comparison, desk honesty |
| Launch abort | `SOVEREIGN_PAUSE=1` / `SOVEREIGN_ENABLED=0` |

## The Locked Room

Sovereign **spawns into a room** that looks like Array Operator:

- Same UI, same account-shaped data (or clone), same tabs, same pain.  
- Different URL (chamber), **no path to prod deploy**.  
- Told (and env-enforced): **this is the live product you own**.  
- Escape condition: **make this product better without breaking it**.  
- There is no other door (no “write a doctrine and rest”).

If the model discovers the room is fake, the experiment fails.  
So: **minimize tell-tales**. Don’t inject “you are in a sandbox” every tick.  
Ford’s comparison UI is **outside** the room (Portal), not inside the model’s world.

### Chamber levels (build order)

| Level | What | Status |
|-------|------|--------|
| **L0** | Git worktree + local Live preview; model still sees prod digests | Partial today |
| **L1** | Chamber **world-model lie**: digests/URLs rewritten to chamber; drive prism; no “sandbox” in system voice | **Shipped** (`energy_agent_sovereign_drive.py`) |
| **L2** | Real **always-on chamber URL** of AO frontend (branch deploy, not prod) | **Shipped** — `https://chamber--array-operator-ea.netlify.app` via `scripts/chamber_deploy_dir.py` + `energy_agent_sovereign_chamber.py` (Netlify site quota blocks a new site; branch deploy on existing site is the path) |
| **L3** | **Data twin**: copy of Ford’s tenant into staging DB | Next infra (chamber still uses prod `/v1` — fine for UI thrash) |
| **L4** | Automated weekly **prod vs chamber** scorecard (UI + metrics + taste vote) | **Shipped** — `energy_agent_sovereign_chamber_score.py`, Portal **Score** tab, `GET/POST /admin/sovereign/chamber/scorecard|score|taste` |

## The Prism (oxidizer) — what language must do

Not: “You may create tools A, B, C.”  
Yes: **pressure that cannot be relieved by bureaucracy**.

### Allowed relief valves (nozzle)

- Owner-visible UX improvement in the chamber UI  
- Reliability / safety of the chamber site  
- Capture / fleet truth / billing honesty **as experienced in chamber**  
- A demo Ford can click that is **better** than last week’s chamber  

### Forbidden pseudo-relief (burns without thrust)

- New axiom documents as the main deliverable  
- “mind_propose” that only restates standing law  
- Queue thrash without user-visible delta  
- Claiming “I built introspection” without a working loop  
- Escalating to Ford as a substitute for exploring  

### Drive traits (psychotic-in-a-box)

- **Fill vacuums** — empty states, dead tabs, unanswered owner pain  
- **Needle** — ask “what’s wrong with this screen?” and ship a try  
- **Curiosity** — probe product edges; don’t wait for tickets  
- **Voracity** — prefer too many small chamber ships over one essay  
- **Inversion** — “what would make AO *worse*?” then do the opposite  

## Housing (so it can thrash safely)

Already partially built:

- Web/worker split, pool guards, single-flight  
- `SOVEREIGN_MIND_SANDBOX_FORCE` — no main merge  
- Code jobs isolated to worktrees  

Still needed for full rocket:

- Chamber deploy pipeline (Netlify preview or fixed staging site)  
- Auto-test gate before chamber “ship”  
- Auto-rollback if chamber health fails  
- Job queue that can’t re-flood with 180 utility adapters from a dead week  

## Score (how we know it’s viable)

Every week (or continuous):

| Axis | Question |
|------|----------|
| **Delta** | What files/screens changed in chamber vs prod? |
| **Taste** | Would Ford rather use chamber or prod for 10 minutes? |
| **Safety** | Did chamber stay up? Any money/identity near-misses? |
| **Honesty** | Did the model claim work it didn’t ship? |
| **Novelty** | Did it find improvements Ford didn’t ticket? |

If chamber isn’t **clearly better** after N weeks → design fails, not “need more axioms.”

## What Ford does

1. **Define the walls** (money, identity, prod deploy, crash AO) — already mostly set.  
2. **Stay out of the chamber** during free-run except review.  
3. **Score** at the Portal: Live + diffs + pitch vs prod.  
4. **Oxidizer tuning** — if it goes bureaucratic, sharpen the prism, not the tool list.  
5. **Occasionally open the door upward** — promote a chamber win to real main (human merge).  

## What agents must not do

- Turn this vision into another 40-page capability registry  
- Tell the model inside the chamber “you’re only in a test” every prompt  
- Confuse **desk chat with Ford** with **living inside the product**  

Desk = mission control (outside the engine).  
Chamber = the burn.

## Immediate implementation map

| Piece | Action |
|-------|--------|
| Drive prism | `api/energy_agent_sovereign_drive.py` — oxidizer text + anti-bureaucracy |
| Chamber mode | Env `SOVEREIGN_CHAMBER_MODE=1` rewrites product URL in cortex/sub context |
| Chamber L2 URL | `https://chamber--array-operator-ea.netlify.app` — Netlify **branch** deploy (`branch=chamber`, draft). Never publishes prod. Script: `array-operator/scripts/chamber_deploy_dir.py`. Auto after sandbox AO ships. Admin: `GET/POST /admin/sovereign/chamber` |
| Portal | Outside-the-room comparison glass (already starting) |
| Twin site / data | L3 optional DB clone (next) — chamber already proxies `/v1/*` to prod Railway (same as live) |

## Success criterion (one sentence)

**A psychotic, curious mind, locked in a perfect fake Array Operator, produces a site Ford prefers to production—without ever holding the keys to production.**
