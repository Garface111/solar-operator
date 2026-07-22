# Sovereign Mind Sandbox

Free-run arena so we can answer: **is Sovereign fundamentally better than Ford at shipping AO, or not?**

## Flow

1. **Start** a run (default 7 days)  
   `POST /admin/sovereign/mind-sandbox/start`  
   `{ "days": 7, "goal": "…" }`
2. Worktrees land under `/root/sovereign-sandbox/<run_id>/{array-operator,solar-operator}`  
   (or `SOVEREIGN_MIND_SANDBOX_ROOT`).
3. **Free-run**  
   - Jobs with `sandbox: true` / kind `sandbox_job`, **or**  
   - `SOVEREIGN_MIND_SANDBOX_FORCE=1` while a run is open  
   → commit **only** in sandbox; **no main merge, no prod deploy**.
4. **Score** anytime: `POST /admin/sovereign/mind-sandbox/score`  
   Compares sandbox ships vs Ford git commits in the same window.
5. **End**: `POST /admin/sovereign/mind-sandbox/end`  
   Writes `scorecard.json` + `comparison.md` under the run dir.

## Cortex

Wake payload includes `mind_sandbox` (active run, doctrine, ends_at).  
Actions: `sandbox_start`, `sandbox_score`, `sandbox_end`.

## Flags

| Env | Default | Meaning |
|-----|---------|---------|
| `SOVEREIGN_MIND_SANDBOX` | `1` | Feature on |
| `SOVEREIGN_MIND_SANDBOX_FORCE` | `0` | All jobs during open run go sandbox |
| `SOVEREIGN_MIND_SANDBOX_ROOT` | `/root/sovereign-sandbox` | Worktree root |
| `SOVEREIGN_MIND_SANDBOX_PUSH_BRANCH` | `0` | Also push sandbox branch to origin (still never main) |

## Scoring (automatic)

Heuristic only — Ford still judges taste:

- volume (jobs/commits)
- breadth (files touched)
- overlap with Ford (shared files)
- sandbox purity (penalize accidental main ships)

Verdicts: `sovereign_edge` · `ford_edge` · `mixed` · `sovereign_only` · `ford_ahead_sovereign_idle` · `inconclusive_no_activity`
