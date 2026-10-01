# Salvage checklist — external skill PRs

Mechanical gates live in `skills/AGENTS.md` and `tests/skills/test_authoring_standards.py`. This checklist is for **salvaging** a large or stale external skill PR without reimplementing it.

## Before you rewrite

1. Confirm target tree: bundled `skills/` vs `optional-skills/` (heavy/niche → optional).
2. Run authoring tests on the skill path: `scripts/run_tests.sh tests/skills/test_authoring_standards.py -q`.
3. Fix **description** ≤ 60 chars, one sentence, period at end.
4. Replace shell-tool names in prose with Hermes tools (`search_files`, `read_file`, `patch`, `terminal`).
5. Credit the human contributor first in `author` frontmatter.

## Salvage merge strategy

- Prefer **cherry-pick / rebase-merge** so original authorship survives.
- Split unrelated changes: skill content vs core wiring — core tool additions need footprint-ladder justification in root `AGENTS.md`.
- Drop compat shims, facade appendages, and change-detector tests; add 1–2 **behavior contract** tests instead.

## Common reject reasons (avoid)

- New core tool when terminal + file or a skill suffices.
- New `HERMES_*` env var for non-secret behavior (use `config.yaml`).
- Lazy `offset`/`limit` on instructional tools (skills, playbooks).
- Third-party SaaS product landed under `plugins/` (standalone plugin repo instead).

## Verification

```bash
scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q
```

Add a focused test file under `tests/skills/` mirroring the skill's category when the skill ships scripts or non-trivial logic.
