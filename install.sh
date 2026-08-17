#!/bin/sh
#
# Install jobchain.
#
# No network access is required or attempted. The only build step is the
# compute-node helper, which needs a C compiler and nothing else.
#
# Usage:
#   ./install.sh                      build and verify in place
#   ./install.sh --prefix /opt/jc     copy the tree there and link the launcher
#   ./install.sh --static             link the helper statically
#   ./install.sh --check              verify an existing installation

set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
PREFIX=""
BUILD_TARGET="all"
CHECK_ONLY=0
STATUS=0

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#--prefix=}"; shift ;;
        --static) BUILD_TARGET="static"; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "install.sh: unknown option $1" >&2; exit 1 ;;
    esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; STATUS=1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
# Some extraction tools and file transfers drop the executable bit. Restore
# it before anything tries to run, so a lost permission is not mistaken for a
# broken installation.
chmod +x "$ROOT/bin/jobchain" "$ROOT/run_tests.sh" "$ROOT/install.sh" 2>/dev/null

say "checking prerequisites"

PYTHON="${JOBCHAIN_PYTHON:-python3}"
if command -v "$PYTHON" >/dev/null 2>&1; then
    say "  python:   $("$PYTHON" --version 2>&1)"
else
    fail "no python3 found; the submit-host tool requires it"
fi

if "$PYTHON" -c "import yaml" 2>/dev/null; then
    say "  pyyaml:   present"
else
    say "  pyyaml:   MISSING - YAML schemas will not load."
    say "            Install PyYAML, or write schemas as Python files instead."
fi

CC="${CC:-cc}"
if command -v "$CC" >/dev/null 2>&1; then
    say "  compiler: $CC"
else
    fail "no C compiler found; set CC to one that exists"
fi

[ "$STATUS" -ne 0 ] && exit 1

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" -eq 0 ]; then
    say "building the compute-node helper"
    ( cd "$ROOT" && make "$BUILD_TARGET" ) || {
        fail "the helper did not build"
        exit 1
    }
fi

if [ ! -x "$ROOT/bin/jobchain-node" ]; then
    fail "bin/jobchain-node is missing or not executable"
    exit 1
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
say "verifying"
say "  helper:   $("$ROOT/bin/jobchain-node" version)"
say "  tool:     $("$ROOT/bin/jobchain" --version 2>&1)"

# The claim protocol depends on mkdir being atomic here. Checking now turns a
# rare mid-run fault into an immediate, understandable failure.
PROBE="${TMPDIR:-/tmp}/jobchain-install-probe-$$"
mkdir -p "$PROBE" || { fail "cannot create a probe directory"; exit 1; }
if "$ROOT/bin/jobchain-node" selftest --home "$PROBE" >/dev/null 2>&1; then
    say "  fs check: ok (on $PROBE)"
    if [ -x "$ROOT/bin/jobchain-node.sh" ]; then
        say "  shell helper present: set JOBCHAIN_NODE to use it instead"
    fi
else
    fail "the filesystem check failed; see 'jobchain-node selftest --home DIR'"
fi
rm -rf "$PROBE"

# ---------------------------------------------------------------------------
# Install to a prefix
# ---------------------------------------------------------------------------
if [ -n "$PREFIX" ]; then
    say "installing to $PREFIX"
    mkdir -p "$PREFIX" || { fail "cannot create $PREFIX"; exit 1; }
    # Only what the submit host and compute nodes actually need at run time:
    # the Python package, the launcher and helpers, examples (the "next
    # steps" message below points at them), and docs. src/ (already compiled
    # into bin/jobchain-node), tests/, Makefile, run_tests.sh, ruff.toml, and
    # DESIGN.md are development-time artifacts that don't belong on every
    # shared-storage install this might be copied to.
    for item in jobchain examples bin README.md CHANGELOG.md \
                install.sh pyproject.toml; do
        [ -e "$ROOT/$item" ] && cp -R "$ROOT/$item" "$PREFIX/"
    done
    chmod +x "$PREFIX/bin/jobchain" "$PREFIX/bin/jobchain-node" \
             "$PREFIX/bin/jobchain-node.sh" "$PREFIX/install.sh" 2>/dev/null
    say ""
    say "Add the launcher to PATH:"
    say "  export PATH=\"$PREFIX/bin:\$PATH\""
    say ""
    say "The helper must be reachable from compute nodes, so $PREFIX"
    say "needs to sit on shared storage."
fi

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------
if [ "$STATUS" -eq 0 ]; then
    TARGET="${PREFIX:-$ROOT}"
    say ""
    say "Installed. To try it against the bundled example:"
    say "  cd $TARGET/examples/pipeline"
    say "  ../../bin/jobchain run solver.yaml --check"
    say ""
    say "Read README.md for the full command reference."
else
    say ""
    say "Installation completed with problems; see the messages above."
fi
exit "$STATUS"
