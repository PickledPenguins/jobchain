#!/usr/bin/env python3
"""Small, dependency-free mutation-testing harness for jobchain.

Each mutation is a deliberate one-line semantic change.  The harness copies
this project to a temporary directory, applies exactly one mutation, runs the
focused unit-test module, and records whether the mutant was killed.

This is intentionally a project-owned category rather than a replacement for
coverage: coverage asks whether code executes; mutation testing asks whether
our assertions detect incorrect behavior.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    old: str
    new: str
    tests: str


MUTATIONS = (
    Mutation(
        "store-done-boundary", "jobchain/store.py",
        "if all(status == DONE for status in statuses):",
        "if all(status != DONE for status in statuses):",
        "tests.test_store_unit",
    ),
    Mutation(
        "store-cancelled-vs-failed", "jobchain/store.py",
        "if stage.status == CANCELLED:",
        "if stage.status == FAILED:",
        "tests.test_store_unit",
    ),
    Mutation(
        "store-running-membership", "jobchain/store.py",
        "if RUNNING in statuses:",
        "if RUNNING not in statuses:",
        "tests.test_store_unit",
    ),
    Mutation(
        "store-queued-claimed-combination", "jobchain/store.py",
        "if QUEUED in statuses or CLAIMED in statuses:",
        "if QUEUED in statuses and CLAIMED in statuses:",
        "tests.test_store_unit",
    ),
    Mutation(
        "store-terminal-done", "jobchain/store.py",
        'return (status == DONE or status.startswith("failed.")',
        'return (status != DONE or status.startswith("failed.")',
        "tests.test_store_unit",
    ),
    Mutation(
        "operations-force-guard", "jobchain/operations.py",
        "if store.exists() and force:",
        "if store.exists() and not force:",
        "tests.test_operations_unit",
    ),
    Mutation(
        "operations-submit-condition", "jobchain/operations.py",
        "if not claimed or submit_only or regenerate or resume:",
        "if claimed and submit_only or regenerate or resume:",
        "tests.test_operations_unit",
    ),
    Mutation(
        "scheduler-cancel-result", "jobchain/scheduler.py",
        "return completed.returncode == 0, output",
        "return completed.returncode != 0, output",
        "tests.test_scheduler_unit",
    ),
    Mutation(
        "scheduler-timeout-error-path", "jobchain/scheduler.py",
        "        except (OSError, subprocess.TimeoutExpired) as exc:\n            raise SchedulerError(\n                f\"could not execute {self.submit_binary}: {exc}\") from exc",
        "        except OSError as exc:\n            raise SchedulerError(\n                f\"could not execute {self.submit_binary}: {exc}\") from exc",
        "fault_injection_tests.run",
    ),
)


def run_mutation(mutation: Mutation, timeout: int) -> bool:
    """Return True when the mutation is killed by its focused tests."""
    with tempfile.TemporaryDirectory(prefix="jobchain-mutant-") as tmp:
        project = Path(tmp) / "project"
        shutil.copytree(
            ROOT,
            project,
            ignore=shutil.ignore_patterns(
                "MagicMock", "htmlcov", ".coverage", "__pycache__"
            ),
        )
        target = project / mutation.file
        source = target.read_text(encoding="utf-8")
        occurrences = source.count(mutation.old)
        if occurrences != 1:
            raise RuntimeError(
                f"{mutation.name}: expected one source match, found {occurrences}"
            )
        target.write_text(source.replace(mutation.old, mutation.new), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "unittest", mutation.tests],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return result.returncode != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    killed = 0
    survived = []
    errors = []
    print("=== mutation testing ===")
    for mutation in MUTATIONS:
        try:
            is_killed = run_mutation(mutation, args.timeout)
        except Exception as exc:  # pragma: no cover - runner infrastructure
            errors.append((mutation.name, str(exc)))
            print(f"ERROR    {mutation.name}: {exc}")
            continue
        if is_killed:
            killed += 1
            print(f"KILLED   {mutation.name}")
        else:
            survived.append(mutation.name)
            print(f"SURVIVED {mutation.name}")

    total = len(MUTATIONS)
    score = killed / total * 100 if total else 100.0
    print(f"\nMutation score: {killed}/{total} ({score:.1f}%)")
    if survived:
        print("Surviving mutants:")
        for name in survived:
            print(f"  - {name}")
    if errors:
        print("Runner errors:")
        for name, error in errors:
            print(f"  - {name}: {error}")
    return 1 if survived or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
