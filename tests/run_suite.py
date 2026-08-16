#!/usr/bin/env python3
"""Run the Python test surface with process isolation and reliable timeouts.

A normal unittest discovery run keeps every test module in one interpreter,
which allows global logging, environment, subprocess, or imported-module
state to leak between modules. This runner first gives each module its own
process. If a module fails or times out, it is rerun class-by-class so a real
failure is distinguished from an order-dependent test-suite contamination.

Coverage is optional and uses parallel data files so independently executed
processes contribute to one later ``coverage combine`` operation.
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib
import inspect
import os
import signal
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_TIMEOUT = 120
DEFAULT_WORKERS = 4


def test_modules(pattern: str) -> list[str]:
    """Return matching test modules in stable order."""
    result = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if fnmatch.fnmatch(path.name, pattern):
            result.append(f"tests.{path.stem}")
    return result


def test_classes(module_name: str) -> list[str]:
    """Return concrete unittest classes defined directly by a module."""
    module = importlib.import_module(module_name)
    result = []
    for name, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ != module.__name__:
            continue
        if not issubclass(cls, unittest.TestCase):
            continue
        if unittest.defaultTestLoader.loadTestsFromTestCase(cls).countTestCases():
            result.append(f"{module_name}.{name}")
    return result


def run_process(target: str, timeout: int, coverage: bool) -> tuple[int, str]:
    """Run one unittest target and return its exit code and captured output."""
    command = [sys.executable]
    if coverage:
        command += [
            "-m", "coverage", "run", "--branch", "--parallel-mode",
            "--source=jobchain",
        ]
    command += ["-m", "unittest", target]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Kill the complete process group. Tests intentionally use subprocesses
        # for schedulers and the node helper; killing only the Python parent
        # can otherwise leave children holding pipes or CPU indefinitely.
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        output = (output or "") + (
            f"\nTEST RUNNER TIMEOUT after {timeout}s: {target}\n"
        )
        return 124, output
    return process.returncode, output or ""


def run_module(module: str, timeout: int, coverage: bool) -> tuple[bool, str]:
    """Run a module; fall back to isolated classes after failure/timeout."""
    code, output = run_process(module, timeout, coverage)
    if code == 0:
        return True, f"--- {module} ---\n{output}"

    lines = [
        f"--- {module} ---",
        output,
        "MODULE FAILED OR TIMED OUT; rerunning classes in isolated processes",
    ]
    classes = test_classes(module)
    if not classes:
        return False, "\n".join(lines)

    class_results: list[tuple[str, int, str]] = []
    # Class-level fallback is deliberately sequential within a module: many
    # integration classes create temporary scheduler processes and filesystem
    # state, so running them concurrently would test a different contract.
    for target in classes:
        class_code, class_output = run_process(target, timeout, coverage)
        class_results.append((target, class_code, class_output))
        lines.append(f"--- isolated {target} ---\n{class_output}")

    failed = any(code != 0 for _, code, _ in class_results)
    return not failed, "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--no-coverage", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    modules = test_modules(args.pattern)
    if not modules:
        print(f"No test modules matched pattern: {args.pattern}", file=sys.stderr)
        return 1

    coverage = not args.no_coverage
    failures = 0
    print(
        f"Running {len(modules)} Python test modules with "
        f"{max(1, args.workers)} isolated workers"
    )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(run_module, module, args.timeout, coverage): module
            for module in modules
        }
        results = []
        for future in as_completed(futures):
            module = futures[future]
            try:
                passed, output = future.result()
            except Exception as exc:  # runner infrastructure failure
                passed = False
                output = f"runner error for {module}: {exc}"
            results.append((module, passed, output))

    for module, passed, output in sorted(results):
        print(output.rstrip())
        if not passed:
            failures += 1
            print(f"FAILED: {module}", file=sys.stderr)

    print(f"Python module results: {len(modules) - failures}/{len(modules)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
