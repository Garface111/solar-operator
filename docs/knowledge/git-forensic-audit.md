# Git Forensic Audit — "did an agent mangle my work?" / "where did my feature go?"

When the user believes another agent (Claude Code, a different Hermes session, a
teammate) DESTROYED or MANGLED their work, or when a feature that "should be
there" appears missing/disconnected — DO NOT speculate, and do NOT start
rebuilding. Run a forensic audit FIRST and report evidence. Most "mangle" panics
turn out to be (a) a deliberate prior removal the user forgot, (b) an orphaned
leftover that was never wired up, or (c) a feature that never actually existed in
code (only in intent/emails/memory). Proving which one it is takes minutes and
prevents hours of rebuilding the wrong thing.

This recipe cracked the June 2026 "a Claude Code agent mangled my Array Operator
work" scare — conclusion: nothing was mangled; Ford himself had removed the
ArrayOverview tab (commit 1d63870), and the "Log in with <inverter>" button never
existed in any commit on any branch.

## The audit sequence (run in order, cheapest signal first)

1. **Working-tree state — is anything uncommitted/lost RIGHT NOW?**
   ```
   git status --short
   git stash list
   ```
   Clean tree + no stashes = nothing was destroyed locally this session. A dirty
   tree means look at WHAT is modified before doing anything else.

2. **Authorship + recency of recent commits — who actually wrote these?**
   ```
   git log -20 --format="%ci  %an  %s"
   ```
   If every recent commit is the USER's own name, no foreign agent rewrote
   history. (Agent branches here are conventionally `agent/*` and land via PR;
   a mangle would show a non-user author or a merge.)

3. **Reflog — were there force-moves, resets, or rebases?**
   ```
   git reflog -15
   ```
   Look for `reset:`, `rebase`, force-move entries. Plain `commit:` / `checkout:`
   lines = normal linear work, no rewrite.

4. **Is the "missing/mangled" file actually tracked, and what's its history?**
   ```
   git ls-files --error-unmatch <path>      # tracked?
   git log --oneline -- <path>              # when added / last touched
   git cat-file -e <tag-or-sha>:<path> && echo exists  # existed at a past tag?
   ```

5. **Is the file ROUTED / IMPORTED, or an orphan?** A file can be fully present
   and committed yet wired to nothing. Grep for its symbol everywhere EXCEPT its
   own definition:
   ```
   git grep -n "<ComponentName>" HEAD -- <src-dir> | grep -v "<file>.tsx:"
   ```
   Zero hits = orphaned leftover (e.g. ArrayOverview.tsx — present but no import,
   no `<Route>`; `/arrays` + `/sandbox` redirect to `/clients`). Orphan ≠ damage.

6. **Did the feature EVER exist? Pickaxe across ALL branches, not just HEAD.**
   The decisive step. `-S` finds any commit that added/removed a string; `--all`
   covers every ref. Loop over branches when you need the per-branch breakdown:
   ```
   git log --all --oneline -S "<distinctive string>" -- <dir>
   # per-branch presence:
   for ref in $(git for-each-ref --format='%(refname)' refs/heads refs/remotes); do
     hits=$(git grep -li "<string>" "$ref" -- <dir> 2>/dev/null)
     [ -n "$hits" ] && { echo "### $ref"; echo "$hits"; }
   done
   ```
   Zero hits across all refs = it was NEVER written in code. (Beware false
   positives from marketing copy / comments — confirm the hit is the real feature,
   not a string mentioning it.)

7. **Was it DELIBERATELY removed? Find the removing commit and read its intent.**
   ```
   git log -S '<code that is now gone>' --oneline -- <path>     # removal commit
   git show <sha> --stat ; git show -s --format="%an %ci%n%s%n%b" <sha>
   ```
   The commit message usually states the WHY ("Remove Arrays tab from dashboard
   nav — drop from TabBar, /arrays redirects to /clients, rebuild dist"). That's
   a decision, not damage.

8. **What's actually DEPLOYED?** Source ≠ served bundle. Check the committed
   dist/build artifact for the feature's strings:
   ```
   git grep -li "<string>" HEAD -- 'api/app_dist/*' 'web/*/dist/*'
   git show HEAD:<bundle.js> | grep -oE "<string>" | sort | uniq -c
   git ls-tree -r --name-only HEAD -- <dist-assets> | grep -i "<Chunk>"
   ```
   A removed lazy chunk absent from dist confirms the disconnect shipped.

## How to report (match Ford's debugging style — evidence, not reassurance-noise)
Lead with the verdict ("NOTHING IS MANGLED" / "you removed it yourself in X").
Then the evidence chain: clean tree, your authorship, no reflog rewrites,
the removing commit + its stated reason, the all-branch pickaxe result. Then
distinguish the three outcomes plainly: deliberate-removal vs orphaned-leftover
vs never-existed — they imply very different next steps. End with the one genuine
open question the audit can't answer from THIS repo (e.g. uncommitted work on a
DIFFERENT machine — this box is a migrated machine, so a feature built elsewhere
and never pushed simply wouldn't be here), and ask before rebuilding.

## Pitfall: don't rebuild on a panic
The user's instinct ("an agent mangled it, fix it") can pull you straight into
re-implementing a feature that was intentionally removed or never existed. Resist.
The audit is fast; rebuilding the wrong artifact is not. Confirm WHAT happened
and get a decision before writing code.
