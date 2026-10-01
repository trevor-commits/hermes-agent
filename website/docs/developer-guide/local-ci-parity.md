# Local CI parity (offline)

Hermes CI runs dozens of lanes; most need activation, network, or a full clone history.
**`scripts/verify_local.sh`** is the fast, offline entry point for the Python static gates
that **block merge** on every PR — the same checks wired in
[`.github/workflows/lint.yml`](https://github.com/NousResearch/hermes-agent/blob/main/.github/workflows/lint.yml)
(`windows-footguns` job) and
[`.github/workflows/lazy-deps-guard.yml`](https://github.com/NousResearch/hermes-agent/blob/main/.github/workflows/lazy-deps-guard.yml).

No `source ./activate`, no API keys, no PyPI. Only **git** and **system `python3`**
(Python **≥ 3.11**, matching `pyproject.toml`; override with `PYTHON=...`).

The wrapper preflights that every bundled checker script exists (fail-fast on a sparse
checkout), prints per-step wall time, and never treats advisory failures as a non-zero exit.

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
(`git fetch --deepen=200 origin main`) and continues.

Advisory steps use **`continue-on-error` parity**: they never fail `verify_local.sh`, even when
`check_public_surface.py` exits `2` (unresolved ref / no merge-base) or prints findings.
Read stdout/stderr like the `windows-footguns` job log in Actions.

Optional CI-style deepen (read-only `git fetch`, needs network):

```bash
VERIFY_FETCH_ADVISORY=1 scripts/verify_local.sh --advisory
```

This mirrors the `for i in 1 2 3` deepen loop in `lint.yml` for PR advisory steps.

### Local `HEAD` vs CI `origin/pr-head`

CI advisory steps diff **`origin/pr-head`** (the PR branch tip fetched from GitHub) against
`origin/<base_ref>`. Locally, `--advisory` diffs **`HEAD`** (your current checkout, including
uncommitted work) against `VERIFY_BASE` (default `origin/main`). That is intentional: you can
run advisory checks on a dirty tree before commit. If local advisory is clean but CI still
prints findings, compare the same refs (`git fetch origin pull/N/head:pr-head` and pass
`VERIFY_BASE=origin/main` with a temporary branch checkout matching the PR).

### Reliability guarantees

The wrapper is designed for unattended cloud-agent and shallow-clone runs:

- **`set -euo pipefail`** — pipeline and unset-variable failures surface immediately.
- **Preflight** — every bundled checker script must exist before any step runs (blocking always;
  advisory scripts are checked only when `--advisory` is passed).
- **Python ≥ 3.11** — matches `pyproject.toml`; fails before slow scans.
- **Interrupt handling** — Ctrl+C prints which step was active and exits `130`.
- **Self syntax-check** — `bash -n` on the wrapper at startup (catches a broken script before
  a 60s compat scan).
- **Advisory never fails the process** — exit `0` when blocking passed, even if advisory steps
  exited non-zero; the closing summary line states `advisory skipped`, `blocking + advisory`,
  or `N advisory step(s) reported issues`.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `PYTHON` | `python3` | Interpreter for check scripts |
| `VERIFY_BASE` | `origin/main` | Base ref for `--advisory` diffs |
| `VERIFY_FETCH_ADVISORY` | unset | Set to `1` to deepen-fetch when merge-base is missing |
| `PYTHONWARNINGS` | `ignore::SyntaxWarning` | Quiets docstring escape noise during compiles |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All **blocking** steps passed (advisory may have printed warnings) |
| `1` | A blocking checker failed, or Python before 3.11 / missing interpreter |
| `2` | Unknown CLI flag (`scripts/verify_local.sh -h` for usage) |

## Offline verify recipe (cloud agent / shallow clone)

No secrets, no merges, no live writes to external services:

```bash
git fetch origin main --depth=50    # read-only; enough for many advisory runs
chmod +x scripts/verify_local.sh
scripts/verify_local.sh
scripts/verify_local.sh --advisory
```

Expect ~1 minute on a full tree for blocking steps (file scans). The script prints
`python=…` and `head=<short-sha>` at start so logs are attributable to a revision.
Per-step timings appear on the `✓` lines (Windows footguns and compat-pointer scans are
usually the slowest).

### Usage-burn draft survey (fork maintenance)

When several cloud-agent drafts touch offline verification, prefer **one canonical PR**
and close superseded siblings. On `trevor-commits/hermes-agent` (Oct 2026 usage-burn lane):

| PR | Branch | Scope |
| --- | --- | --- |
| **#10** | `cursor/usage-burn-deeper-reliability-1458` | **Canonical** — `verify_local.sh` + this doc |
| **#9** | `cursor/usage-burn-verify-local-d116` | Superseded by #10 |
| **#8** | `cursor/agents-harness-hygiene-6fd8` | Orthogonal harness docs (`CLAUDE.md`, `hermes-agent-dev` skill) |

List open drafts: `gh pr list --repo trevor-commits/hermes-agent --draft --state open`.

## Not covered by `verify_local.sh`

These still require activation, network, path-specific changes, or long runtimes:

| Lane | When it runs | Local command / notes |
| --- | --- | --- |
| ruff + ty diff (advisory) | PRs only | `lint.yml` `lint-diff` job; not bundled locally |
| Pytest suite | Python changes | `scripts/run_tests.sh` |
| JS / Vitest | Desktop, TUI, dashboard | `npm test` in the owning workspace |
| `uv lock --check` | `pyproject.toml` / `uv.lock` | `hermes pm lock` after dependency edits |
| Icon freshness | Asset SVG changes | `node scripts/generate-icons.mjs` then commit outputs |
| Infographic / profile-artifact guards | Specific paths | See workflow names in `.github/workflows/ci.yaml` |
| OS-marked pytest lanes | Host-specific tests | Full suite on macOS / Windows as needed |
| Desktop E2E | Desktop changes | CI disposable hosts; not a local default |

The orchestrator job **All required checks pass** (`ci.yaml`) gates merge; use the table above
to decide what else to run for your diff. The change classifier
(`scripts/ci/classify_changes.py`) skips some lanes when only docs or frontend files change —
locally, still run `verify_local.sh` before any push (case-collision and footgun gates are
never skipped in CI).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `SyntaxWarning: invalid escape sequence` during a step | CPython 3.12+ warns while compiling tracked files | Benign noise; the check still passes. Warnings are suppressed in `verify_local.sh` for readability. |
| `--ruff` errors: ruff not found | Ruff not on PATH / not installed | `python -m scripts.ci.python_packages ruff==0.15.10` or `scripts/verify_local.sh` without `--ruff` |
| Advisory skips: no merge-base | Shallow clone or branch diverged | `git fetch --deepen=200 origin main` |
| `error: missing checker script` at start | Sparse checkout or wrong branch | `git checkout` the feature branch; confirm `scripts/check_*.py` exist |
| Case collision / compat failures | New import path or filename clash | Read the checker stdout; fix the reported path |
| Tests pass locally but fail in CI | Bare `pytest`, credentials set, or shared `HERMES_HOME` | Always use `scripts/run_tests.sh` |
| `--advisory` fails the script | Older `verify_local.sh` treated advisory like blocking | Upgrade: advisory steps must not fail the wrapper (see above) |
| `public-surface: cannot resolve ref` | Shallow clone / base not fetched | `git fetch origin main` or `VERIFY_FETCH_ADVISORY=1` |
| Summary says `advisory skipped` | No `--advisory`, missing base ref, or no merge-base | Pass `--advisory`, `git fetch origin main`, or `VERIFY_FETCH_ADVISORY=1` |
| Local advisory clean, CI advisory noisy | CI diffs `origin/pr-head`; local diffs `HEAD` | Align refs (see **Local HEAD vs CI pr-head** above) |
