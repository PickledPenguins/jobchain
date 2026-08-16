#!/usr/bin/env python3
"""Measure line coverage of jobchain/ for one or more test modules, using
the standard library's trace module (coverage.py could not be installed
in this environment: no network access to PyPI). Not branch-accurate like
coverage.py, but a real, per-line count of what each category of test
actually exercises.

Usage:
    python3 tools_measure_coverage.py <label> <test.module.name> [more...]

Writes a JSON summary to coverage-reports/<label>.json and prints a short
report to stdout.
"""
from __future__ import annotations

import ast
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
JOBCHAIN_DIR = os.path.join(PROJECT_ROOT, "jobchain")

_WORKER = r'''
import sys, os, sysconfig, trace, unittest, json
sys.path.insert(0, {project_root!r})

module_names = {module_names!r}

# Ignore only the standard library and site-packages, found via sysconfig
# rather than derived from sys.path: deriving it from sys.path (which
# includes this worker script's own directory as sys.path[0]) causes
# trace.Trace to under-count jobchain/__init__.py and __main__.py to 0%
# regardless of what actually runs, for reasons not fully understood even
# after isolating the exact minimal reproduction -- using sysconfig's
# paths directly avoids the problem entirely and is what this depends on.
_stdlib = sysconfig.get_paths()["stdlib"]
_purelib = sysconfig.get_paths()["purelib"]
ignoredirs = [_stdlib, _purelib]
tracer = trace.Trace(count=True, trace=False, ignoredirs=ignoredirs)

def run():
    # A direct `import jobchain` here, before the loader pulls it in
    # transitively via `from jobchain import ...` inside a test module,
    # is also required: trace.Trace does not record execution that
    # happens only as a side effect of unittest.TestLoader's own import
    # machinery (confirmed empirically), so without this jobchain's
    # module-level code would always read as uncovered.
    import jobchain
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in module_names:
        suite.addTests(loader.loadTestsFromName(name))
    runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
    return runner.run(suite)

result = tracer.runfunc(run)
counts = tracer.results().counts
# Only keep entries under jobchain/, to keep the handoff file small.
jobchain_dir = os.path.join({project_root!r}, "jobchain")
filtered = {{f"{{f}}:{{l}}": c for (f, l), c in counts.items()
           if os.path.abspath(f).startswith(jobchain_dir + os.sep)}}
with open({outfile!r}, "w") as fh:
    json.dump({{"tests_run": result.testsRun,
               "failures": len(result.failures),
               "errors": len(result.errors),
               "counts": filtered}}, fh)
'''


def executable_lines(path: str) -> set[int]:
    """Line numbers in a module that could execute: every statement's
    first line, per the AST. Excludes blanks, comments, and continuation
    lines, so the denominator matches what a human would call "a line of
    code" reasonably closely.
    """
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    lines: set[int] = set()

    class Visitor(ast.NodeVisitor):
        def generic_visit(self, node):
            if isinstance(node, ast.stmt) and not isinstance(node, ast.Module):
                lines.add(node.lineno)
            super().generic_visit(node)

    Visitor().visit(tree)
    return lines


def measure(label: str, module_names: list[str]) -> dict:
    """Run module_names in a fresh subprocess with line tracing enabled,
    so jobchain (and everything it imports) is guaranteed to execute for
    the first time inside the traced region -- module-level code (import
    statements, constants) is only ever hit once per process, so any
    reuse of an already-imported jobchain from an outer process would
    under-count it.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        outfile = tmp.name
    try:
        script = _WORKER.format(project_root=PROJECT_ROOT,
                                module_names=module_names, outfile=outfile)
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(
                f"coverage worker for {label!r} failed:\n{proc.stdout}\n{proc.stderr}")
        with open(outfile) as f:
            worker_result = json.load(f)
    finally:
        if os.path.exists(outfile):
            os.remove(outfile)

    counts = worker_result["counts"]
    per_module = {}
    total_executable = 0
    total_hit = 0
    for fname in sorted(os.listdir(JOBCHAIN_DIR)):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(JOBCHAIN_DIR, fname)
        try:
            exec_lines = executable_lines(path)
        except SyntaxError:
            continue
        hit_lines = set()
        for key, c in counts.items():
            f, _, ln = key.rpartition(":")
            if c > 0 and os.path.abspath(f) == os.path.abspath(path):
                hit_lines.add(int(ln))
        hit = len(exec_lines & hit_lines)
        total = len(exec_lines)
        per_module[fname] = {
            "executable_lines": total,
            "hit_lines": hit,
            "percent": round(100 * hit / total, 1) if total else 0.0,
        }
        total_executable += total
        total_hit += hit

    return {
        "label": label,
        "modules": module_names,
        "tests_run": worker_result["tests_run"],
        "failures": worker_result["failures"],
        "errors": worker_result["errors"],
        "per_module": per_module,
        "total_executable_lines": total_executable,
        "total_hit_lines": total_hit,
        "total_percent": round(100 * total_hit / total_executable, 1)
                         if total_executable else 0.0,
    }


if __name__ == "__main__":
    label = sys.argv[1]
    modules = sys.argv[2:]
    summary = measure(label, modules)
    os.makedirs(os.path.join(PROJECT_ROOT, "coverage-reports"), exist_ok=True)
    outpath = os.path.join(PROJECT_ROOT, "coverage-reports", f"{label}.json")
    with open(outpath, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"{label}: {summary['tests_run']} tests run, "
          f"{summary['failures']} failures, {summary['errors']} errors, "
          f"{summary['total_percent']}% line coverage of jobchain/ "
          f"({summary['total_hit_lines']}/{summary['total_executable_lines']} lines)")
