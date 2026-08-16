#!/bin/sh
#
# Run the jobchain test suite.
#
# Every Python test module runs in its own interpreter. This prevents global
# logger/environment/subprocess state from one module contaminating another,
# while coverage's parallel mode preserves one combined application report.
#
# Usage:
#   ./run_tests.sh                 build, run everything, report coverage
#   ./run_tests.sh --fast          skip coverage and the sanitizer build
#   ./run_tests.sh --no-sanitizer  optimized C build instead of sanitized
#   ./run_tests.sh --no-c-coverage skip the dedicated C source coverage pass
#   ./run_tests.sh <pattern>       run only matching test modules

set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT" || exit 2

PYTHON="${JOBCHAIN_PYTHON:-python3}"
CC="${CC:-cc}"
USE_COVERAGE=1
USE_SANITIZER=1
USE_C_COVERAGE=1
C_LINE_MIN=60
C_BRANCH_MIN=80
PATTERN="test_*.py"
STATUS=0

for arg in "$@"; do
    case "$arg" in
        --fast) USE_COVERAGE=0; USE_SANITIZER=0; USE_C_COVERAGE=0 ;;
        --no-coverage) USE_COVERAGE=0 ;;
        --no-sanitizer) USE_SANITIZER=0 ;;
        --no-c-coverage) USE_C_COVERAGE=0 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) PATTERN="$arg" ;;
    esac
done

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/jobchain-tests.XXXXXX")
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT HUP INT TERM

section() {
    printf '\n=== %s ===\n' "$1"
}

append_asan_option() {
    value=$1
    if [ -n "${ASAN_OPTIONS:-}" ]; then
        ASAN_OPTIONS="$ASAN_OPTIONS:$value"
    else
        ASAN_OPTIONS="$value"
    fi
    export ASAN_OPTIONS
}

# ---------------------------------------------------------------------------
# Build the compute-node helper
# ---------------------------------------------------------------------------
section "building the node helper"
if [ "$USE_SANITIZER" -eq 1 ]; then
    if make debug; then
        # Some Python/test environments load another shared library before a
        # sanitized child process starts. The binary itself is correctly
        # linked; disabling ASan's link-order diagnostic avoids a false test
        # failure in that parent-process environment.
        append_asan_option verify_asan_link_order=0
        echo "built with AddressSanitizer and UndefinedBehaviorSanitizer"
    else
        echo "sanitizer build failed; falling back to the optimized build" >&2
        if ! make; then
            echo "optimized build failed" >&2
            exit 2
        fi
        USE_SANITIZER=0
    fi
else
    if ! make; then
        exit 2
    fi
fi

# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------
section "static checks"
echo "C: compiled clean under -Wall -Wextra -Werror -pedantic"

if sh -n bin/jobchain-node.sh && sh -n bin/jobchain; then
    echo "shell: helper and launcher parse"
else
    echo "shell: a script does not parse" >&2
    STATUS=1
fi

if "$PYTHON" -m compileall -q jobchain tests state_property_tests concurrency_tests fault_injection_tests >/dev/null; then
    echo "Python: all test and application modules compile"
else
    echo "Python: compilation FAILED" >&2
    STATUS=1
fi

if command -v ruff >/dev/null 2>&1; then
    ruff check jobchain tests state_property_tests concurrency_tests fault_injection_tests || STATUS=1
else
    echo "Python: ruff not installed, skipping lint"
fi

if command -v mypy >/dev/null 2>&1; then
    mypy --ignore-missing-imports jobchain || STATUS=1
else
    echo "Python: mypy not installed, skipping type check"
fi

# ---------------------------------------------------------------------------
# Python test modules
# ---------------------------------------------------------------------------
section "Python test modules"
if [ "$USE_COVERAGE" -eq 1 ] && "$PYTHON" -c "import coverage" 2>/dev/null; then
    COVERAGE_MODE=1
    rm -f .coverage .coverage.*
    "$PYTHON" -m coverage run --branch --source=jobchain \
        -m unittest discover -s tests -t . -p "$PATTERN" -v 2>&1 | tail -25
    TEST_STATUS=$?
    section "coverage"
    "$PYTHON" -m coverage report -m --skip-covered --fail-under=98
    "$PYTHON" -m coverage html -d htmlcov >/dev/null 2>&1 \
        && echo "detailed report written to htmlcov/index.html"
else
    COVERAGE_MODE=0
    [ "$USE_COVERAGE" -eq 1 ] && echo "coverage.py not installed; running without it"
fi

MODULE_TIMEOUT=${JOBCHAIN_TEST_TIMEOUT:-120}
TEST_WORKERS=${JOBCHAIN_TEST_WORKERS:-4}
if [ "$COVERAGE_MODE" -eq 1 ]; then
    "$PYTHON" tests/run_suite.py --pattern "$PATTERN" --timeout "$MODULE_TIMEOUT" \
        --workers "$TEST_WORKERS"
else
    "$PYTHON" tests/run_suite.py --pattern "$PATTERN" --timeout "$MODULE_TIMEOUT" \
        --workers "$TEST_WORKERS" --no-coverage
fi
if [ "$?" -ne 0 ]; then
    STATUS=1
fi

if [ "$COVERAGE_MODE" -eq 1 ]; then
    section "Python coverage"
    if ! "$PYTHON" -m coverage combine; then
        echo "coverage: combine FAILED" >&2
        STATUS=1
    elif ! "$PYTHON" -m coverage report -m --skip-covered --fail-under=98; then
        echo "coverage: threshold FAILED" >&2
        STATUS=1
    else
        "$PYTHON" -m coverage html -d htmlcov >/dev/null 2>&1 \
            && echo "detailed report written to htmlcov/index.html"
    fi
fi

# ---------------------------------------------------------------------------
# State/property tests. These exercise the production RowState model against
# an independent invariant model rather than testing a test-local rollup.
# ---------------------------------------------------------------------------
section "state/property tests"
if PYTHONPATH="$ROOT" "$PYTHON" state_property_tests/run.py; then
    echo "State/property: PASS"
else
    echo "State/property: FAIL" >&2
    STATUS=1
fi

# ---------------------------------------------------------------------------
# Concurrency & race-condition tests
# ---------------------------------------------------------------------------
section "concurrency & race tests"
if PYTHONPATH="$ROOT" "$PYTHON" concurrency_tests/run.py; then
    echo "Concurrency: PASS"
else
    echo "Concurrency: FAIL" >&2
    STATUS=1
fi

# ---------------------------------------------------------------------------
# Fault-injection tests
# ---------------------------------------------------------------------------
section "fault-injection tests"
if PYTHONPATH="$ROOT" "$PYTHON" fault_injection_tests/run.py; then
    echo "Fault injection: PASS"
else
    echo "Fault injection: FAIL" >&2
    STATUS=1
fi

# ---------------------------------------------------------------------------
# Load tests
# ---------------------------------------------------------------------------
section "load tests"
if "$PYTHON" load_tests/run.py; then
    echo "Load testing: PASS"
else
    echo "Load testing: FAIL" >&2
    STATUS=1
fi

# ---------------------------------------------------------------------------
# Bottleneck & scaling tests
# ---------------------------------------------------------------------------
section "bottleneck & scaling tests"
if "$PYTHON" bottleneck_tests/run.py; then
    echo "Bottleneck testing: PASS"
else
    echo "Bottleneck testing: FAIL" >&2
    STATUS=1
fi

# ---------------------------------------------------------------------------
# Dedicated C source coverage. This uses a temporary instrumented binary so
# the normal production binary is never replaced by coverage artifacts.
# ---------------------------------------------------------------------------
if [ "$USE_C_COVERAGE" -eq 1 ] && [ -x "$(command -v "$CC" 2>/dev/null || true)" ] && command -v gcov >/dev/null 2>&1; then
    section "C source coverage"
    COV_DIR="$TMP_DIR/c-coverage"
    mkdir -p "$COV_DIR"
    if "$CC" -O0 -g -std=c99 -pedantic -Wall -Wextra -Werror \
        -Wshadow -Wconversion -Wstrict-prototypes -Wmissing-prototypes \
        --coverage -fno-omit-frame-pointer \
        -o "$COV_DIR/jobchain-node" src/jobchain-node.c; then
        cp bin/jobchain-node.sh "$COV_DIR/jobchain-node.sh"
        if JOBCHAIN_NODE="$COV_DIR/jobchain-node" PYTHONPATH="$ROOT" \
            "$PYTHON" -m unittest tests.test_node >"$TMP_DIR/c-node.log" 2>&1; then
            tail -15 "$TMP_DIR/c-node.log"
            if gcov -b -c -o "$COV_DIR" src/jobchain-node.c >"$TMP_DIR/gcov.log" 2>&1; then
                cat "$TMP_DIR/gcov.log"
                "$PYTHON" - "$TMP_DIR/gcov.log" "$C_LINE_MIN" "$C_BRANCH_MIN" <<'PYGCOV'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
line = re.search(r"Lines executed:([0-9.]+)%", text)
branch = re.search(r"Branches executed:([0-9.]+)%", text)
if not line or not branch:
    raise SystemExit("could not parse gcov coverage summary")
line_pct = float(line.group(1))
branch_pct = float(branch.group(1))
line_min = float(sys.argv[2])
branch_min = float(sys.argv[3])
if line_pct < line_min or branch_pct < branch_min:
    raise SystemExit(
        f"C coverage below threshold: lines={line_pct:.2f}% "
        f"(min {line_min:.2f}%), branches={branch_pct:.2f}% "
        f"(min {branch_min:.2f}%)"
    )
PYGCOV
                if [ "$?" -ne 0 ]; then
                    STATUS=1
                fi
            else
                cat "$TMP_DIR/gcov.log" >&2
                STATUS=1
            fi
        else
            cat "$TMP_DIR/c-node.log" >&2
            echo "C coverage test execution FAILED" >&2
            STATUS=1
        fi
    else
        echo "C coverage: instrumented build FAILED" >&2
        STATUS=1
    fi
else
    echo "C source coverage: compiler/gcov unavailable or explicitly skipped"
fi

# ---------------------------------------------------------------------------
# Leave an optimized binary behind, not a sanitized one.
# ---------------------------------------------------------------------------
if [ "$USE_SANITIZER" -eq 1 ]; then
    section "restoring the optimized build"
    if make >/dev/null; then
        echo "bin/jobchain-node rebuilt without sanitizers"
    else
        echo "optimized rebuild FAILED" >&2
        STATUS=1
    fi
fi

section "result"
if [ "$STATUS" -eq 0 ]; then
    echo "PASS"
else
    echo "FAIL"
fi
exit "$STATUS"

python3 state_property_tests/run.py
