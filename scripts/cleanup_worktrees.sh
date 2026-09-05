#!/usr/bin/env bash
set -euo pipefail

# Finds and, on explicit request, removes Claude Code worktrees (.claude/worktrees/<name>/) whose
# work is both (a) flagged done by the user (scripts/mark_worktree_done.sh's .session-done marker)
# and (b) fully merged into origin/main -- see docs/environment.md's "Multi-agent worktrees" section
# and docs/collaboration.md's "Recommend branch cleanup at session closeout" section for why both
# conditions matter: merged-but-unmarked can still be someone's live work (a merge mid-session
# doesn't mean the session is over), and marked-but-unmerged risks discarding real commits.
#
# Usage:
#   scripts/cleanup_worktrees.sh list
#       Read-only. Prints two groups: worktrees eligible for default deletion (marked done AND
#       merged), and other candidates (missing one of those two conditions) shown for visibility
#       only -- never delete those without the user explicitly choosing them by name.
#
#   scripts/cleanup_worktrees.sh delete [--all-marked] [<name> ...]
#       Removes the named worktree(s): `git worktree remove`, deletes the local branch and its
#       origin copy (if present), and removes the per-worktree Docker image
#       (trntest-lunar-demo-<name>). `--all-marked` expands to every worktree currently eligible for
#       default deletion (re-checked fresh at delete time, not reused from an earlier `list` call).
#       Every name is re-verified merged into origin/main right before deletion regardless of how it
#       was selected -- this script will never delete a branch with unmerged commits. Never removes
#       the main checkout or the worktree it's currently being run from. Does not touch a worktree's
#       generated output/ directory -- clean that up separately if you don't need it.
#
# All deletion is opt-in per worktree: this script never deletes anything on its own (e.g. via cron)
# and `delete` always requires the caller to name what to remove, whether individually or via
# --all-marked -- both are meant to follow the user explicitly confirming the list from `list`.

usage() {
    echo "Usage: $0 list" >&2
    echo "       $0 delete [--all-marked] [<name> ...]" >&2
    exit 1
}

[[ $# -ge 1 ]] || usage
cmd="$1"
shift

repo_root="$(git rev-parse --show-toplevel)"
common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
main_checkout="$(dirname "$common_dir")"
self_root="$repo_root"

git fetch origin main --quiet 2>/dev/null \
    || echo "Warning: 'git fetch origin main' failed -- merge checks below may use a stale" \
        "origin/main." >&2

# Populates the parallel arrays wt_name/wt_path/wt_branch/wt_marked/wt_merged/wt_ahead for every
# worktree except the main checkout, the worktree this script is running from, and any detached
# (branchless) worktree -- none of those are ever cleanup candidates.
collect_worktrees() {
    wt_name=(); wt_path=(); wt_branch=(); wt_marked=(); wt_merged=(); wt_ahead=()
    local cur_path="" cur_branch=""
    while IFS= read -r line; do
        if [[ "$line" == worktree\ * ]]; then
            cur_path="${line#worktree }"
            cur_branch=""
        elif [[ "$line" == branch\ * ]]; then
            cur_branch="${line#branch refs/heads/}"
        elif [[ -z "$line" ]]; then
            if [[ -n "$cur_path" && -n "$cur_branch" \
                && "$cur_path" != "$main_checkout" && "$cur_path" != "$self_root" ]]; then
                local marked=0 merged=0 ahead=0
                [[ -f "$cur_path/.session-done" ]] && marked=1
                if git merge-base --is-ancestor "$cur_branch" origin/main 2>/dev/null; then
                    merged=1
                else
                    ahead="$(git rev-list --count "origin/main..$cur_branch" 2>/dev/null || echo '?')"
                fi
                wt_name+=("$(basename "$cur_path")")
                wt_path+=("$cur_path")
                wt_branch+=("$cur_branch")
                wt_marked+=("$marked")
                wt_merged+=("$merged")
                wt_ahead+=("$ahead")
            fi
            cur_path=""; cur_branch=""
        fi
    done < <(git worktree list --porcelain; echo)
}

print_list() {
    collect_worktrees
    local any_default=0 any_candidate=0
    echo "Marked for deletion (session flagged done, branch fully merged):"
    for i in "${!wt_name[@]}"; do
        if [[ "${wt_marked[$i]}" == 1 && "${wt_merged[$i]}" == 1 ]]; then
            local marked_at
            marked_at="$(sed -n 's/^marked_at=//p' "${wt_path[$i]}/.session-done" 2>/dev/null)"
            echo "  ${wt_name[$i]}  (branch ${wt_branch[$i]}, marked ${marked_at:-unknown time})"
            any_default=1
        fi
    done
    [[ "$any_default" == 1 ]] || echo "  (none)"
    echo
    echo "Other candidates (not deleted unless explicitly named):"
    for i in "${!wt_name[@]}"; do
        if [[ "${wt_marked[$i]}" == 1 && "${wt_merged[$i]}" == 1 ]]; then
            continue
        fi
        local reason
        if [[ "${wt_marked[$i]}" == 0 && "${wt_merged[$i]}" == 0 ]]; then
            reason="no .session-done marker; branch is ${wt_ahead[$i]} commit(s) ahead of origin/main"
        elif [[ "${wt_marked[$i]}" == 0 ]]; then
            reason="no .session-done marker (branch is merged)"
        else
            reason="marked done, but branch is ${wt_ahead[$i]} commit(s) ahead of origin/main"
        fi
        echo "  ${wt_name[$i]}  (branch ${wt_branch[$i]}) -- $reason"
        any_candidate=1
    done
    [[ "$any_candidate" == 1 ]] || echo "  (none)"
}

do_delete() {
    collect_worktrees
    local -a targets=()
    local all_marked=0
    for arg in "$@"; do
        if [[ "$arg" == "--all-marked" ]]; then
            all_marked=1
        else
            targets+=("$arg")
        fi
    done
    if [[ "$all_marked" == 1 ]]; then
        for i in "${!wt_name[@]}"; do
            if [[ "${wt_marked[$i]}" == 1 && "${wt_merged[$i]}" == 1 ]]; then
                targets+=("${wt_name[$i]}")
            fi
        done
    fi
    if [[ "${#targets[@]}" -eq 0 ]]; then
        echo "Nothing to delete (no names given and no --all-marked matches)." >&2
        exit 1
    fi

    for name in "${targets[@]}"; do
        local found=-1
        for i in "${!wt_name[@]}"; do
            [[ "${wt_name[$i]}" == "$name" ]] && { found="$i"; break; }
        done
        if [[ "$found" == -1 ]]; then
            echo "Skipping '$name': not a known worktree (or it's the main checkout / this script's" \
                "own worktree)." >&2
            continue
        fi
        local path="${wt_path[$found]}" branch="${wt_branch[$found]}"
        # Re-verify merge status fresh, right before deleting -- never trust the earlier snapshot
        # for the actual destructive step.
        if ! git merge-base --is-ancestor "$branch" origin/main 2>/dev/null; then
            echo "Refusing to delete '$name': branch '$branch' has commits not in origin/main." >&2
            continue
        fi
        echo "Removing worktree '$name' ($path, branch $branch)..."
        git worktree remove --force "$path"
        git branch -D "$branch"
        if git ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
            git push origin --delete "$branch"
        fi
        local image="trntest-lunar-demo-$name"
        if docker image inspect "$image" >/dev/null 2>&1; then
            docker rmi "$image"
        fi
        echo "Done with '$name'."
    done
}

case "$cmd" in
    list) print_list ;;
    delete) do_delete "$@" ;;
    *) usage ;;
esac
