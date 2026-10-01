# Local CI parity (offline)

Hermes CI runs dozens of lanes; most need activation, network, or a full clone history.
**`scripts/verify_local.sh`** is the fast, offline entry point for the Python static gates
that **block merge** on every PR — the same checks wired in
[`.github/workflows/lint.yml`](https://github.com/NousResearch/hermes-agent/blob/main/.github/workflows/lint.yml)
(`windows-footguns` job) and
[`.github/workflows/lazy-deps-guard.yml`](https://github.com/NousResearch/hermes-agent/blob/main/.github/workflows/lazy-deps-guard.yml).

No `source ./activate`, no API keys, no PyPI. Only **git** and **system `python3`** (override with `PYTHON=...`).

## Quick start

```bash
chmod +x scripts/verify_local.sh   # once per clone if the execute bit is missing
scripts/verify_local.sh
scripts/verify_local.sh --ruff      # optional; same blocking `ruff check .` as CI
scripts/verify_local.sh --advisory  # optional; PR-style advisory diffs when merge-base exists
```

Typical order before a push:

1. `scripts/verify_local.sh` (add `--ruff` when ruff is installed)
2. `source ./activate` (or set `HERMES_PYTHON` to a PM-built test interpreter)
3. `scripts/run_tests.sh` for the paths you touched

## Parity matrix (blocking static gates)

| `verify_local.sh` step | CI workflow | CI job / step |
| --- | --- | --- |
| plugin-compat pointers | `lint.yml` | `windows-footguns` → Forbid in-tree use of plugin-compat pointers |
| lazy_deps imports | `lazy-deps-guard.yml` | `check` → Check tools.lazy_deps imports |
| case-colliding paths | `case-collision-check.yml` | `check-case-collisions` |
| portable Bash shebangs | `lint.yml` | `windows-footguns` → Require portable Bash shebangs |
| Windows footguns | `lint.yml` | `windows-footguns` → Run footgun checker |
| scratch-path literals | `lint.yml` | `windows-footguns` → Forbid hard-coded scratch paths |
| config.yaml writers | `lint.yml` | `windows-footguns` → Forbid config.yaml writers |
| OS marker fakes in tests | `lint.yml` | `windows-footguns` → Forbid unmarked macOS fakes |
| `ruff check .` (`--ruff`) | `lint.yml` | `ruff-blocking` |

Install the same ruff pin CI uses:

```bash
python -m scripts.ci.python_packages ruff==0.15.10
```

## Advisory checks (`--advisory`)

On pull requests, CI also runs **advisory** steps inside `lint.yml` (they never fail the job):

- Profile-scope hazard patterns on added lines (`scripts/check_profile_scope_patterns.py`)
- Public-surface diff vs base (`scripts/ci/check_public_surface.py`)

Locally:

```bash
git fetch origin main
scripts/verify_local.sh --advisory
# or pin another base:
VERIFY_BASE=origin/main scripts/verify_local.sh --advisory
```

If `origin/main` is missing or there is no merge-base yet, the script prints a skip hint
(`git fetch --deepen=200 origin main`) and continues. Advisory scripts always exit 0;
read their output like a reviewer would in the Actions log.

## Not covered by `verify_local.sh`

These still require activation, network, path-specific changes, or long runtimes:

| Lane | When it runs | Local command / notes |
| --- | --- | --- |
| Pytest suite | Python changes | `scripts/run_tests.sh` |
| JS / Vitest | Desktop, TUI, dashboard | `npm test` in the owning workspace |
| `uv lock --check` | `pyproject.toml` / `uv.lock` | `hermes pm lock` after dependency edits |
| Icon freshness | Asset SVG changes | `node scripts/generate-icons.mjs` then commit outputs |
| Infographic / profile-artifact guards | Specific paths | See workflow names in `.github/workflows/ci.yaml` |
| OS-marked pytest lanes | Host-specific tests | Full suite on macOS / Windows as needed |
| Desktop E2E | Desktop changes | CI disposable hosts; not a local default |

The orchestrator job **All required checks pass** (`ci.yaml`) gates merge; use the table above
to decide what else to run for your diff. The change classifier in CI skips some lanes when
only docs or frontend files change — locally, still run `verify_local.sh` before any push.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `SyntaxWarning: invalid escape sequence` during a step | CPython 3.12+ warns while compiling tracked files | Benign noise; the check still passes. Warnings are suppressed in `verify_local.sh` for readability. |
| `--ruff` errors: ruff not found | Ruff not on PATH / not installed | `python -m scripts.ci.python_packages ruff==0.15.10` or `scripts/verify_local.sh` without `--ruff` |
| Advisory skips: no merge-base | Shallow clone or branch diverged | `git fetch --deepen=200 origin main` |
| Case collision / compat failures | New import path or filename clash | Read the checker stdout; fix the reported path |
| Tests pass locally but fail in CI | Bare `pytest`, credentials set, or shared `HERMES_HOME` | Always use `scripts/run_tests.sh` |
