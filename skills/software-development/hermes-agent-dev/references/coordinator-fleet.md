# Coordinator fleet (Cursor Cloud Agents and similar)

Multiple autonomous agents often work the same hermes-agent checkout (Cursor Cloud Agents, internal coordinator runs, or kanban-orchestrated profiles). Keep collisions predictable.

## One branch, one PR

- Each agent run uses its **own** git branch (`cursor/<topic>-<suffix>` on Cloud Agents).
- Commit and push on that branch only; open **one draft PR per branch**. Do not reuse another agent's branch without explicit handoff.
- Before merging, rebase or reset onto current `main` if the branch sat idle — squash merges from stale branches silently drop recent fixes.

## Shared rules, not duplicated prose

- **Root `AGENTS.md`** is the canonical contributor contract for this repo.
- **`CLAUDE.md`** at the repo root is intentionally thin and `@`-imports `AGENTS.md` for Claude Code; do not fork long rule blocks into `CLAUDE.md`.
- Area **`AGENTS.md`** files (`agent/`, `tools/`, `hermes_cli/`, …) extend the root file for that subtree only.

## Hermes profile fleet (different concern)

Running **multiple Hermes profiles** or a **kanban worker fleet** on one host is documented in `website/docs/user-guide/multi-profile-gateways.md` and `website/docs/user-guide/features/kanban-worker-lanes.md`. That is runtime isolation (config, sessions, secrets), not the same as IDE/cloud agent branch hygiene — but the same rule applies: **do not assume another profile's cwd or secrets**.

## Cloud Agent environment

- Use the repo's **`source ./activate`** / PM workflow; do not raw `pip install` into Hermes-managed venvs.
- Tests: `scripts/run_tests.sh` with temp `HERMES_HOME` (runner sets this; never write to real `~/.hermes/` in tests).
- No secrets in commits, PR bodies, or logs; `.env` is for credentials only, not behavioral config.

## When two agents touch the same subsystem

Prefer **serial ownership**: one agent owns gateway, another owns docs-only, etc. If both must edit the same files, merge `main` frequently and keep PRs small so review can see overlap early.
