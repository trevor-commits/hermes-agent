# Hermes Agent — development context

**Canonical rules:** [`AGENTS.md`](./AGENTS.md) at this repository root (area-specific `AGENTS.md` files under each top-level directory).

**Hermes sessions** load project context in priority order: `.hermes.md` → `AGENTS.md` (directory chain) → `CLAUDE.md` → `.cursorrules`. In this repo, **`AGENTS.md` wins** over this file when both exist at the cwd.

**Claude Code** loads this file; keep it thin and delegate detail to `AGENTS.md`:

@AGENTS.md

For in-repo workflow (tests, PR salvage, Cursor Cloud Agent fleet), preload the bundled skill: `hermes -s hermes-agent-dev` (see `skills/software-development/hermes-agent-dev/SKILL.md`).
