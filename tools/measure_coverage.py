#!/usr/bin/env python3
"""Run a named test category under coverage and print machine-readable metrics.

The category lists are intentionally explicit. This prevents a new test file
from silently changing the meaning of a historical coverage number.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "coverage-reports"

CATEGORIES = {
    "unit": [
        "tests.test_validators",
        "tests.test_validator_matrix_extended",
        "tests.test_config",
        "tests.test_pipeline",
        "tests.test_schema_scan",
        "tests.test_report_scheduler",
        "tests.test_errors",
        "tests.unit.test_operations_helpers",
        "tests.unit.test_store_unit",
        "tests.unit.test_store_deep",
        "tests.unit.test_core_unit",
        "tests.unit.test_schema_loading",
        "tests.unit.test_pipeline_deep",
        "tests.unit.test_scheduler_deep",
        "tests.unit.test_operations_remaining",
        "tests.unit.test_config_remaining",
        "tests.unit.test_operations_deep_remaining",
        "tests.unit.test_operations_gap_closure",
        "tests.unit.test_operations_final_gaps",
        "tests.unit.test_cli_deep",
        "tests.unit.test_parse_exhaustive",
        "tests.unit.test_report_exhaustive",
        "tests.unit.test_schema_exhaustive",
        "tests.unit.test_store_exhaustive",
        "tests.unit.test_operations_exhaustive",
        "tests.unit.test_main_module",
        "tests.unit.test_config_exhaustive",
        "tests.unit.test_scheduler_exhaustive",
        "tests.unit.test_pipeline_exhaustive",
    ],
    "smoke": ["tests.test_examples", "tests.test_example_matrix"],
    "regression": [
        "tests.test_negative_examples",
        "tests.test_operational_matrix",
        "tests.test_reconciliation_matrix",
        "tests.test_concurrency_operations",
        "tests.test_scheduler_fixture",
    ],
    "integration": ["tests.test_integration_exhaustive", "tests.test_schema_scan", "tests.test_pipeline", "tests.test_config", "tests.test_report_scheduler", "tests.test_validators", "tests.test_validator_matrix_extended", "tests.test_errors"],
    "e2e": ["tests.test_cli", "tests.test_additional_examples"],
    "performance": ["tests.test_load_examples"],
}


def run(category: str) -> int:
    tests = CATEGORIES[category]
    OUT.mkdir(exist_ok=True)
    data = OUT / f"{category}.json"
    cov = ROOT / ".coverage"
    if cov.exists():
        cov.unlink()
    for partial in ROOT.glob(".coverage.*"):
        partial.unlink()

    # Run each explicitly listed target in its own process.  Apart from making
    # coverage aggregation more deterministic, this isolates integration tests
    # that intentionally exercise process/scheduler lifecycle behavior.
    exit_code = 0
    for test in tests:
        command = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            "--branch",
            "--source=jobchain",
            "-m",
            "unittest",
            test,
            "-q",
        ]
        result = subprocess.run(command, cwd=ROOT)
        exit_code = exit_code or result.returncode

    partials = list(ROOT.glob(".coverage.*"))
    if partials:
        subprocess.run(
            [sys.executable, "-m", "coverage", "combine"],
            cwd=ROOT,
            check=True,
        )
    if cov.exists():
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", str(data)],
            cwd=ROOT,
            check=True,
        )
        report = json.loads(data.read_text())
        totals = report["totals"]
        functions = [
            function
            for file in report["files"].values()
            for function in file["functions"].values()
        ]
        executed_functions = sum(bool(f.get("executed_lines")) for f in functions)
        function_total = len(functions)
        function_pct = (
            100.0 * executed_functions / function_total if function_total else 100.0
        )
        metrics = {
            "category": category,
            "tests": tests,
            "test_exit_code": exit_code,
            "line_percent": totals["percent_covered"],
            "statement_percent": totals["percent_statements_covered"],
            "function_percent": function_pct,
            "branch_percent": totals["percent_branches_covered"],
            "condition_percent": None,
            "path_percent": None,
            "condition_note": "coverage.py branch mode does not instrument condition coverage separately",
            "path_note": "bounded path model not yet implemented",
        }
        print(json.dumps(metrics, indent=2, sort_keys=True))

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("category", choices=sorted(CATEGORIES))
    args = parser.parse_args()
    return run(args.category)


if __name__ == "__main__":
    raise SystemExit(main())
