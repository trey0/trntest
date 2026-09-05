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
