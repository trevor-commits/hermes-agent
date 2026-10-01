---
name: hermes-agent-dev
description: "Hermes-agent repo workflow: tests, PRs, scope, fleet."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes-agent, contributing, tests, pr, profile-scope, cloud-agents]
    category: software-development
    related_skills: [hermes-agent, github, hermes-agent-skill-authoring]
---

# Hermes Agent — in-tree development

Use this skill when editing **NousResearch/hermes-agent** (or a fork kept in sync). It does not replace the repo rules — it routes you to them.

## When to Use

- Fixing bugs, writing tests, or opening PRs in the hermes-agent checkout
- Navigating the facade + sibling layout or profile-scope binding rules
- Running Cursor Cloud Agents (or other coordinator fleets) against the same repo without stepping on each other

## Prerequisites

- Dev environment: `source ./activate` from the repo root; isolate `HERMES_HOME` / `HERMES_RUNTIME_DIR` per `website/docs/reference/package-management.md#developer-workflow`
- Tests: **`scripts/run_tests.sh`** only (never bare `pytest` on a credentialed shell)

## How to Run

1. Read root **`AGENTS.md`**; before editing a subtree, read that directory's **`AGENTS.md`** (routing table at the bottom of the root file).
2. For quick layout and conventions, `skill_view` the **`hermes-agent`** skill file `references/contributor-guide.md`.
3. For parallel cloud/coordinator agents, read **`references/coordinator-fleet.md`**.
4. For external skill PR salvage, read **`references/new-skill-pr-salvage.md`**.

## Quick Reference

| Task | Read |
|---|---|
| Tool / toolset / registry | `tools/AGENTS.md` |
| CLI / slash / profiles | `hermes_cli/AGENTS.md` |
| Turn loop / caching | `agent/AGENTS.md` |
| Gateway / profile scope | `gateway/AGENTS.md` |
| Skill authoring HARDLINE | `skills/AGENTS.md` |
| Cloud agent fleet hygiene | `references/coordinator-fleet.md` |

## Procedure

- Reproduce on current `main`, point to the failing line, fix the bug class (sibling paths included).
- Patch where production reads (facade binding, not the defining module, when tests monkeypatch).
- Keep core narrow; prefer skills, CLI + skill, `check_fn`, plugins, MCP, then new core tools.
- One PR per branch; push before opening/updating the PR.

## Pitfalls

- Hardcoding `~/.hermes` — use `get_hermes_home()` / `display_hermes_home()`.
- Breaking prompt caching or role alternation mid-conversation.
- `os.environ.copy()` for child spawns under multiplex — use profile-scoped env helpers.
- Referencing removed sections in root `AGENTS.md` (e.g. "Adding New Tools") — use area guides instead.

## Verification

```bash
scripts/run_tests.sh tests/<area>/test_<topic>.py -q
```

Run the narrowest file that covers your change before pushing.
