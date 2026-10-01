# Project Context Files

Hermes injects project-level instructions into the system prompt by reading context files from the working directory. The discovery order is **first match wins** — only one project context source is loaded per session.

| File (in priority order) | Discovery | Use when |
|---|---|---|
| `.hermes.md` / `HERMES.md` | Walks parents up to the git root, stops at git root | You want hierarchical project rules (root + per-package overrides) |
| `AGENTS.override.md` | Per directory on the git-root → cwd chain (wins over `AGENTS.md` in that directory) | Personal override next to committed `AGENTS.md` (typically gitignored) |
| `AGENTS.md` / `agents.md` | **Merged chain** from git root down to cwd; progressive discovery in subdirs during the session | Portable agent instructions (Hermes, Codex, OpenCode, …); deeper files add more specific rules |
| `CLAUDE.md` / `claude.md` | Cwd only (shadowed when `.hermes.md` or the AGENTS chain loads) | Claude Code; prefer a thin file that `@`-imports `AGENTS.md` in shared repos |
| `.cursorrules` / `.cursor/rules/*.mdc` | Cwd only | Migrating from Cursor |

`SOUL.md` (in `$HERMES_HOME`) is independent and always loaded when present — it sets the agent's identity, not project rules.

### Pick the right one

- **Use `.hermes.md`** when you want Hermes-specific behavior that lives above the cwd (root + subtree), or when you want rules to inherit from a parent directory. The parent walk stops at the git root, so a home-level `.hermes.md` won't leak into every project (a git repo's root is the boundary).
- **Use `AGENTS.md`** when the same project will also be worked on by other agents (Codex, Claude Code, OpenCode). In Hermes, repo-wide rules live at the git root and package-specific rules in nested directories; only **one** project-context *type* loads per session (`.hermes.md` beats the AGENTS chain beats `CLAUDE.md`).
- **Use thin `CLAUDE.md` + `@AGENTS.md`** when Claude Code is primary but Hermes/Codex should share the same prose (see the hermes-agent repo root for the pattern).
- **Don't put project rules in `~/.hermes/AGENTS.md`** (or any other home-level location). When Hermes runs with that directory as cwd, the file loads — but only for that one directory. For cross-project context, use `SOUL.md` (in `$HERMES_HOME`, identity-only) or install a skill via `hermes skills install`.

### Size and truncation

Each context file is capped at 20,000 characters. Files longer than that get **head + tail** truncated (the middle is dropped, with a `[...truncated...]` marker). For large project rules, prefer splitting into multiple skills over cramming one file.

### Security

All context files pass through the threat-pattern scanner before reaching the system prompt. Patterns matching prompt injection or promptware are replaced with a `[BLOCKED: ...]` placeholder. This means an `AGENTS.md` containing obvious injection attempts won't reach the model — the scanner blocks the content, not the file, so the rest of the file still loads.

### Disable for one session

`hermes --ignore-rules` skips auto-injection of all project context files (`.hermes.md`, `AGENTS.md`, `CLAUDE.md`, `.cursorrules`) **and** `SOUL.md` identity, plus user config, plugins, and MCP servers. Use it to isolate whether a problem is your setup or Hermes itself.

### Example: a small `.hermes.md`

```markdown
# My Project

Hermes: when working in this repo, follow these rules.

## Build
- Always run `make test` before declaring a change done.
- Use `uv run` for Python, not `pip install`.

## Style
- Prefer `pathlib.Path` over `os.path`.
- No `print()` in production code — use the `logger`.
```

That file at `/home/me/projects/myrepo/.hermes.md` is auto-loaded when Hermes runs in any subdirectory of `/home/me/projects/myrepo`, but not when it runs in `/home/me/other-project`.
