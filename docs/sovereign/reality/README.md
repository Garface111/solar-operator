# Sovereign Reality File

**Cold hard truth** of Array Operator product changes — frontend, backend, ops.

| File | Role |
|------|------|
| `CHANGELOG.jsonl` | Append-only. One JSON object per line. Never rewrite. |
| `INDEX.md` | Regenerated rolling summary for cortex wake. |
| This README | How the mind uses the file. |

## Who writes

| Source | When |
|--------|------|
| `git` / `ford` / `agent` | `scripts/seed_sovereign_reality.py` or `POST /admin/sovereign/reality/seed` |
| `sovereign` | Code worker after a live ship |
| `sovereign_sandbox` | Code worker after a mind-sandbox free-run ship |
| `ford` | Manual `POST /admin/sovereign/reality/append` or cortex `reality_record` |

## Who reads

Every **cortex wake** (`build_think_prompt` → `reality_file`): doctrine + INDEX + last N entries  
(token budget controlled by `SOVEREIGN_REALITY_WAKE_TAIL` / `SOVEREIGN_REALITY_WAKE_CHARS`).

## Seed

```bash
cd ~/solar-operator
.venv/bin/python scripts/seed_sovereign_reality.py --since 2026-05-01
```

## Line shape

```json
{
  "ts": "2026-07-21T…",
  "source": "ford|sovereign|agent|git|bot|sovereign_sandbox",
  "repos": ["array-operator"],
  "summary": "Command Center: Ask-the-fleet AI",
  "files": ["public/command-center.js"],
  "surfaces": ["frontend"],
  "sha": "…",
  "job_id": "job_…",
  "why": "optional"
}
```
