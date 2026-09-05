#!/usr/bin/env bash
set -euo pipefail

# Marks the current worktree as having no more open work -- the explicit, user-authored signal
# scripts/cleanup_worktrees.sh looks for before ever proposing to delete a worktree by default. See
# docs/collaboration.md's "Recommend branch cleanup at session closeout" section: an agent runs this
# when the user says they're done with the session, in place of the "close session -> delete
# worktree?" prompt Claude Code's CLI has but Claude Desktop doesn't.
#
# Run from within the worktree being marked (not the main checkout). Writes a gitignored
# .session-done file at the worktree root; safe to re-run (overwrites the timestamp/note).
# This alone does not make a worktree eligible for automatic deletion -- its branch must also be
# fully merged into origin/main (checked by cleanup_worktrees.sh, not here), so marking a worktree
# with unmerged work done just flags it for the user's attention instead.

repo_root="$(git rev-parse --show-toplevel)"
common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
# .git's parent is the main checkout root regardless of which worktree this runs from -- see
# scripts/setup_worktree_docker_env.sh for the same derivation.
main_checkout="$(dirname "$common_dir")"

if [[ "$repo_root" == "$main_checkout" ]]; then
    echo "Refusing to mark the main checkout done -- this is for worktree checkouts only." >&2
    exit 1
fi

marker="$repo_root/.session-done"
{
    echo "marked_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [[ $# -gt 0 ]]; then
        echo "note=$*"
    fi
} > "$marker"

echo "Marked $repo_root as done ($marker)."
echo "'scripts/cleanup_worktrees.sh list' (run from any worktree) will offer it for deletion once" \
    "its branch is merged into origin/main."
