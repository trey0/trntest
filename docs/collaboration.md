# Collaboration conventions

How a Claude Code session should collaborate with the user on this repo — commit/merge timing,
what to do with valuable ad hoc exploration, and how to present findings. Distinct from `AGENTS.md`
itself, which covers tooling/codebase conventions.

## Review before commit

Hold off on `git commit` for any change to notebook output, plotting code, or other user-facing
rendering until the user has had a chance to look at it live — start the Jupyter Lab server
(`docker compose up -d` from `docker/`) and point them at the regenerated notebook, rather than
committing right after a lint+test pass alone.

## Review before merge

When pushing a multi-file task to a branch for review, work in batches of up to ~3 files (fewer for
very large ones), pushing each batch to the branch as you go. Don't `git push origin <branch>:main`
until the user has reviewed that specific batch and given the go-ahead — an earlier approval for one
batch doesn't carry over to the next.

## Recommend branch cleanup at session closeout

When the user brings up wrapping up a session, proactively recommend cleaning up this session's
worktree/branch. Left alone, these accumulate as cruft — both on this VPS (worktree checkouts,
several GB each, plus their own Docker images) and on `origin` (stale branches). This is a
closeout-time recommendation, not a mid-session rule: deleting/recreating branches to start new work
within one agent's own session isn't worth worrying about.

Claude Code's CLI prompts to delete a session's worktree when you close it; Claude Desktop has no
equivalent event, so nothing ever proposes cleanup on its own there. This repo's stand-in: run
`scripts/mark_worktree_done.sh` in the closing session's own worktree once its work is merged into
`main` — this is the explicit "I'm done here" signal the CLI's close button would otherwise provide.
A *different*, later session (never the one being closed — it can't safely remove its own worktree)
runs `scripts/cleanup_worktrees.sh list` to see what's now safe to remove (marked done *and* fully
merged) versus other candidates that are missing one of those two conditions, then
`scripts/cleanup_worktrees.sh delete ...` to actually remove a worktree, its local + `origin`
branch, and its Docker image together. Only ever delete what the user has confirmed from that list —
a worktree merged but not marked, or marked but not merged, might still be someone's live or
resumable work.

## Preserve valuable spikes

Ad hoc exploration (one-off `docker compose run` commands, scratch scripts under `src/scratch/`,
inline snippets) is the right way to investigate quickly. Once it lands on a result worth keeping,
consolidate it into a real module in `src/trntest/` (docstrings/tests at this repo's usual bar) and
a real jupytext notebook — don't leave it as shell history. If the result is a genuine new
capability but not yet validated, consider a dedicated branch rather than merging straight to
`main`.

## Show findings, don't just describe them

When investigating something visual (a plot, an overlay, a diagnostic image), put the actual
image/plot where it can be opened and viewed — a live notebook cell, a throwaway scratch notebook is
fine — rather than only generating it for inspection via a file-read tool and describing it in
prose.

## Show the derivation for algorithm proposals

When proposing a change to a numerical algorithm (a brightness-matching scheme, a stretch/
normalization rule, a scoring formula), work through the actual algebra and confirm the change is
real — not cancelled out by a downstream step, e.g. a re-derived display stretch — before presenting
it as a fix.
