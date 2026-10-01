#!/usr/bin/env bash
# Fast offline checks that mirror CI blocking gates (lint.yml, lazy-deps-guard).
# Uses only system python3 and git — no PM activation, no network, no API keys.
#
# Parity table: website/docs/developer-guide/local-ci-parity.md
#
# For the full pytest suite (needs activate or HERMES_PYTHON with pytest):
#   scripts/run_tests.sh
#
# Usage:
#   scripts/verify_local.sh                    # static checks (default)
#   scripts/verify_local.sh --ruff               # also run `ruff check .` when ruff is installed
#   scripts/verify_local.sh --advisory           # profile-scope + public-surface (needs merge-base)
#   scripts/verify_local.sh --ruff --advisory
#   scripts/verify_local.sh -h|--help
#
# Optional env:
#   PYTHON                 interpreter for check scripts (default: python3)
#   VERIFY_BASE            base ref for --advisory (default: origin/main)
#   VERIFY_FETCH_ADVISORY  set to 1 to run CI-style deepen fetches when merge-base is missing (needs network)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
VERIFY_BASE="${VERIFY_BASE:-origin/main}"
RUN_RUFF=0
RUN_ADVISORY=0
ADVISORY_ISSUES=0
# skipped | ran_clean | ran_with_issues
ADVISORY_STATE="skipped"
CURRENT_STEP=""

# Blocking check scripts (must exist before we start; fail fast with a clear path).
BLOCKING_SCRIPTS=(
  scripts/check_compat_pointers.py
  scripts/ci/check_lazy_deps_imports.py
  scripts/check-case-collisions.py
  scripts/check_bash_shebangs.py
  scripts/check-windows-footguns.py
  scripts/check_no_tmp_literals.py
  scripts/check_config_yaml_writers.py
  scripts/ci/check_os_marker_fakes.py
)

ADVISORY_SCRIPTS=(
  scripts/check_profile_scope_patterns.py
  scripts/ci/check_public_surface.py
)

preflight_scripts() {
  local -n scripts=$1
  local label="$2"
  local miss=0
  for rel in "${scripts[@]}"; do
    if [[ ! -f "$REPO_ROOT/$rel" ]]; then
      echo "error: missing ${label} script: $rel" >&2
      miss=1
    fi
  done
  if [[ "$miss" -ne 0 ]]; then
    echo "error: incomplete checkout — re-clone or git checkout the branch you intend to verify" >&2
    exit 1
  fi
}

# Compiling some tracked modules emits SyntaxWarning on escape sequences in docstrings;
# CI uses the same sources — suppress noise so failures stand out.
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::SyntaxWarning}"

usage() {
  cat <<'EOF'
Usage: scripts/verify_local.sh [--ruff] [--advisory]

Offline static checks aligned with CI blocking jobs (see local-ci-parity.md):
  - plugin-compat pointer imports
  - tools.lazy_deps production imports
  - case-colliding tracked paths
  - portable Bash shebangs
  - Windows footgun patterns (all tracked Python)
  - hard-coded scratch-path literals (baseline burn-down)
  - config.yaml writers outside atomic_config_write
  - unmarked macOS fakes in tests

Options:
  --ruff       Run `ruff check .` (lint.yml ruff-blocking job)
  --advisory   Run profile-scope + public-surface diffs (lint.yml advisory; always exit 0)

Install ruff: python -m scripts.ci.python_packages ruff==0.15.10

Advisory steps never fail this script (same as lint.yml continue-on-error).
Set VERIFY_FETCH_ADVISORY=1 to deepen-fetch when merge-base is missing (network).

Tests and typecheck are out of scope; use scripts/run_tests.sh after
source ./activate (or set HERMES_PYTHON to a PM-built test interpreter).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ruff) RUN_RUFF=1; shift ;;
    --advisory) RUN_ADVISORY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: PYTHON interpreter not found: $PYTHON" >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "error: Hermes requires Python >= 3.11 (got: $("$PYTHON" -V 2>&1))" >&2
  exit 1
fi

if ! git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: not a git work tree: $REPO_ROOT" >&2
  exit 1
fi

cd "$REPO_ROOT"

if ! bash -n "$SCRIPT_DIR/verify_local.sh" 2>/dev/null; then
  echo "error: verify_local.sh failed bash -n syntax check" >&2
  exit 1
fi

on_interrupt() {
  echo "" >&2
  if [[ -n "$CURRENT_STEP" ]]; then
    echo "verify_local: interrupted during: ${CURRENT_STEP}" >&2
  else
    echo "verify_local: interrupted" >&2
  fi
  exit 130
}
trap on_interrupt INT TERM

STARTED_AT=$SECONDS
HEAD_SHORT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "verify_local: python=$("$PYTHON" -V 2>&1 | tr -d '\n') head=${HEAD_SHORT}"

preflight_scripts BLOCKING_SCRIPTS blocking

run_step() {
  local title="$1"
  shift
  local ec=0
  local step_start=$SECONDS
  CURRENT_STEP="$title"
  echo ""
  echo "▶ ${title}"
  if "$@"; then
    echo "  ✓ ${title} ($((SECONDS - step_start))s)"
  else
    ec=$?
    echo "  ✗ ${title} failed (exit ${ec})" >&2
    echo "  hint: website/docs/developer-guide/local-ci-parity.md § Troubleshooting" >&2
    exit "${ec}"
  fi
  CURRENT_STEP=""
}

# Advisory steps mirror lint.yml continue-on-error: never fail verify_local.
run_advisory_step() {
  local title="$1"
  shift
  local ec=0
  CURRENT_STEP="$title"
  echo ""
  echo "▶ ${title}"
  if "$@"; then
    echo "  ✓ ${title}"
  else
    ec=$?
    ADVISORY_ISSUES=$((ADVISORY_ISSUES + 1))
    echo "  ⚠ ${title} exited ${ec} (advisory — read output; job continues)" >&2
  fi
  CURRENT_STEP=""
}

ensure_advisory_merge_base() {
  local base="$VERIFY_BASE"
  if git merge-base "$base" HEAD >/dev/null 2>&1; then
    return 0
  fi
  if [[ "${VERIFY_FETCH_ADVISORY:-0}" != "1" ]]; then
    return 1
  fi
  local branch="${base#origin/}"
  if [[ "$branch" == "$base" ]]; then
    echo "▶ advisory fetch (VERIFY_FETCH_ADVISORY=1): deepen ${base} (non-origin ref)"
    git fetch --no-tags --deepen=200 "$base" 2>/dev/null || true
  else
    echo "▶ advisory fetch (VERIFY_FETCH_ADVISORY=1): deepen origin ${branch}"
    git fetch --no-tags --deepen=200 origin "$branch" 2>/dev/null || true
  fi
  local i
  for i in 1 2 3; do
    git merge-base "$base" HEAD >/dev/null 2>&1 && return 0
    if [[ "$branch" != "$base" ]]; then
      git fetch --no-tags --deepen=1000 origin "$branch" 2>/dev/null || true
    fi
  done
  return 1
}

run_step "plugin-compat pointers" "$PYTHON" scripts/check_compat_pointers.py
run_step "lazy_deps imports" "$PYTHON" scripts/ci/check_lazy_deps_imports.py
run_step "case-colliding paths" "$PYTHON" scripts/check-case-collisions.py
run_step "portable Bash shebangs" "$PYTHON" scripts/check_bash_shebangs.py
run_step "Windows footguns" "$PYTHON" scripts/check-windows-footguns.py --all
run_step "scratch-path literals" "$PYTHON" scripts/check_no_tmp_literals.py
run_step "config.yaml writers" "$PYTHON" scripts/check_config_yaml_writers.py
run_step "OS marker fakes in tests" "$PYTHON" scripts/ci/check_os_marker_fakes.py

if [[ "$RUN_RUFF" -eq 1 ]]; then
  if command -v ruff >/dev/null 2>&1; then
    run_step "ruff check ." ruff check .
  elif "$PYTHON" -c 'import ruff' 2>/dev/null; then
    run_step "ruff check ." "$PYTHON" -m ruff check .
  else
    echo ""
    echo "error: --ruff requested but ruff is not installed (try: python -m scripts.ci.python_packages ruff==0.15.10)" >&2
    exit 1
  fi
fi

if [[ "$RUN_ADVISORY" -eq 1 ]]; then
  preflight_scripts ADVISORY_SCRIPTS advisory
  if ! git rev-parse --verify "$VERIFY_BASE" >/dev/null 2>&1; then
    ADVISORY_STATE="skipped_no_ref"
    echo ""
    echo "▶ advisory checks (skipped: ${VERIFY_BASE} not found — run: git fetch origin main)"
  elif ! ensure_advisory_merge_base; then
    ADVISORY_STATE="skipped_no_merge_base"
    echo ""
    echo "▶ advisory checks (skipped: no merge-base with ${VERIFY_BASE})"
    echo "  hint: git fetch --deepen=200 origin main"
    echo "  or:  VERIFY_FETCH_ADVISORY=1 scripts/verify_local.sh --advisory  # CI-style deepen (network)"
  else
    ADVISORY_STATE="ran_clean"
    run_advisory_step "profile-scope patterns (advisory)" \
      "$PYTHON" scripts/check_profile_scope_patterns.py --base "$VERIFY_BASE" --head HEAD
    run_advisory_step "public-surface diff (advisory)" \
      "$PYTHON" scripts/ci/check_public_surface.py --base "$VERIFY_BASE" --head HEAD
    if [[ "$ADVISORY_ISSUES" -gt 0 ]]; then
      ADVISORY_STATE="ran_with_issues"
    fi
  fi
fi

trap - INT TERM
elapsed=$((SECONDS - STARTED_AT))
echo ""
case "$ADVISORY_STATE" in
  skipped)
    echo "✓ verify_local: all blocking checks passed (${elapsed}s); advisory not requested"
    ;;
  skipped_no_ref)
    echo "✓ verify_local: blocking passed (${elapsed}s); advisory skipped (${VERIFY_BASE} missing)"
    ;;
  skipped_no_merge_base)
    echo "✓ verify_local: blocking passed (${elapsed}s); advisory skipped (no merge-base with ${VERIFY_BASE})"
    ;;
  ran_with_issues)
    echo "✓ verify_local: blocking passed; ${ADVISORY_ISSUES} advisory step(s) reported issues (${elapsed}s)"
    ;;
  ran_clean)
    echo "✓ verify_local: blocking + advisory checks passed (${elapsed}s)"
    ;;
esac
