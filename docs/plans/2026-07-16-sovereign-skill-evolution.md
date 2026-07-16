# Sovereign skill evolution

**Date:** 2026-07-16  
**Status:** shipped

## Hermes / research mapping

| Hermes | Sovereign |
|--------|-----------|
| SKILL.md on disk | `ea_sovereign_skills` table |
| Progressive disclosure (index → body) | `skills.index` + `skills.loaded` in cortex prompt |
| Create after complex success | Job `done` → upsert + evolution harvest |
| Patch on error recovery | Job `failed` + LLM evolve from traces |
| Curator | Deprecate low quality / unused |
| skill_manage create/patch | `upsert_skill` + `/admin/sovereign/skills/evolve` |
| DSPy+GEPA self-evolution | Grok/Claude rewrite from real traces (same loop, our LLMs) |

## Ops

- List: `GET /admin/sovereign/skills`
- Get: `GET /admin/sovereign/skills/{name}`
- Evolve now: `POST /admin/sovereign/skills/evolve?force=true`
- Status: `GET /admin/sovereign/skills-status`
- Kill: `SOVEREIGN_SKILLS=0` / `SOVEREIGN_SKILL_EVOLVE=0`
- Scheduler: every 20 min `energy_agent_sovereign_skills`
- Stress: `scripts/stress_sovereign_skills.py`
