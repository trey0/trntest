#!/usr/bin/env bash

# Claude Code SessionStart hook (wired up in .claude/settings.json). Its stdout is injected into
# the session's context. Replaces two AGENTS.md "remember to do this first" rules with something the
# harness runs every time: (1) generate this checkout's docker/.env via
# setup_worktree_docker_env.sh, so the first `docker compose` call can't write into the shared,
# un-namespaced output/; (2) remind the agent to call ListAgents, which a hook can't call itself.
# Also fires on resume/clear/compact -- harmless, since the setup script is idempotent and keeps
# this worktree's existing Jupyter port.

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

echo "[SessionStart hook: scripts/claude_session_start.sh]"
if setup_output="$(scripts/setup_worktree_docker_env.sh 2>&1)"; then
    echo "Docker env already set up for this checkout (no need to run setup_worktree_docker_env.sh yourself):"
    echo "$setup_output" | grep -E '^(Main checkout|Wrote|COMPOSE_PROJECT_NAME|TRNTEST_HOST_OUTPUT_DIR|TRNTEST_JUPYTER_PORT)'
else
    echo "WARNING: scripts/setup_worktree_docker_env.sh failed -- run it yourself and fix before any docker compose call:"
    echo "$setup_output"
fi
echo
echo "Reminder: call ListAgents now (per AGENTS.md), and announce your worktree/branch and plan to any peers."
exit 0
