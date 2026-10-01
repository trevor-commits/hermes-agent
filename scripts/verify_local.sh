#!/usr/bin/env bash
# Fast offline checks that mirror CI blocking gates (lint.yml, lazy-deps-guard).
# Uses only system python3 and git — no PM activation, no network, no API keys.
#
# For the full pytest suite (needs activate or HERMES_PYTHON with pytest):
#   scripts/run_tests.sh
#
# Usage:
#   scripts/verify_local.sh              # static checks (default)
#   scripts/verify_local.sh --ruff       # also run `ruff check .` when ruff is installed
#   scripts/verify_local.sh -h|--help
#
# Optional env:
#   PYTHON   interpreter for check scripts (default: python3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
RUN_RUFF=0

usage() {
  cat <<'EOF'
Usage: scripts/verify_local.sh [--ruff]

Offline static checks aligned with CI blocking jobs:
  - plugin-compat pointer imports
  - tools.lazy_deps production imports
  - case-colliding tracked paths
  - portable Bash shebangs
  - Windows footgun patterns (all tracked Python)
  - hard-coded scratch-path literals (baseline burn-down)
  - config.yaml writers outside atomic_config_write
  - unmarked macOS fakes in tests

Add --ruff to run `ruff check .` (same as the lint workflow blocking job).
Install ruff locally or use: python -m scripts.ci.python_packages ruff==0.15.10

Tests and typecheck are out of scope here; use scripts/run_tests.sh after
source ./activate (or set HERMES_PYTHON to a PM-built test interpreter).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ruff) RUN_RUFF=1; shift ;;
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

cd "$REPO_ROOT"

run_step() {
  local title="$1"
  shift
  echo ""
  echo "▶ ${title}"
  "$@"
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

echo ""
echo "✓ verify_local: all checks passed"
