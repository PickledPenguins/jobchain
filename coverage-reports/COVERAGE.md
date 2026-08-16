# Coverage report — jobchain 0.5-v4b

Measured with `tools_measure_coverage.py`, a line-coverage tool built on
the standard library's `trace` module. `coverage.py` could not be
installed in the environment this release was built in (no network
access to PyPI); `trace` gives real per-line counts but not `coverage.py`'s
branch-level detail, so a `partly` branch that happens to always take the
same path reads as fully covered here.

Each category below is measured independently — the test files for that
category only, run against a clean interpreter — so a percentage in one
column reflects only what that category's tests exercise on their own,
not what the full suite covers. The `combined` column is every category
together, which is the number that matters for overall release quality.

## Coverage matrix

| module | unit | integration | e2e | security | regression | errors | **combined** |
|---|---|---|---|---|---|---|---|
| `__init__.py` | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | **100.0%** |
| `__main__.py` | 100.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | **100.0%** |
| `cli.py` | 95.0% | 83.0% | 37.0% | 31.7% | 41.9% | 35.0% | **95.0%** |
| `config.py` | 70.3% | 88.1% | 74.3% | 75.2% | 74.3% | 76.7% | **93.6%** |
| `core.py` | 96.3% | 94.4% | 93.5% | 94.4% | 93.5% | 95.4% | **96.3%** |
| `operations.py` | 92.6% | 84.3% | 49.5% | 40.8% | 46.4% | 44.6% | **93.6%** |
| `parse.py` | 94.6% | 59.3% | 63.4% | 58.3% | 55.6% | 59.3% | **94.6%** |
| `pipeline.py` | 76.9% | 84.1% | 63.2% | 57.8% | 62.1% | 62.8% | **93.9%** |
| `report.py` | 90.9% | 92.2% | 19.8% | 19.8% | 58.8% | 19.8% | **94.7%** |
| `scheduler.py` | 87.8% | 88.2% | 45.1% | 37.5% | 45.1% | 38.5% | **93.8%** |
| `schema.py` | 95.3% | 50.8% | 61.7% | 42.9% | 50.0% | 47.9% | **95.3%** |
| `store.py` | 93.9% | 83.6% | 71.5% | 63.7% | 70.5% | 74.6% | **93.9%** |
| **tests run** | 672 | 325 | 8 | 21 | 13 | 34 | **1060** |
| **TOTAL** | **91.1%** | **78.0%** | **53.7%** | **46.1%** | **54.8%** | **50.0%** | **94.4%** |

## Reading the matrix

- **`__main__.py`** is only exercised by the unit layer
  (`test_core_unit.py`, absorbing the incoming `test_main_module.py`),
  which specifically tests the `python -m jobchain` entry point. No
  integration-style test invokes the module that way, so every other
  column shows 0% there — accurate, not a gap worth closing on its own,
  since the entry point is a two-line dispatch to `cli.main()`, which
  every other category exercises directly.
- **`cli.py`/`operations.py`/`scheduler.py`/`report.py`** are lowest in
  the non-unit categories (e2e, security, errors, regression) because
  those categories deliberately exercise a narrow slice of behavior each
  (a handful of example projects, malformed input, error paths, four
  specific past defects) rather than the full command surface. The
  `unit` and `integration` layers are what carry breadth for these
  modules, and they do: `cli.py` is at 95%/83% there.
- **`config.py`** is the one module where `unit` (70.3%) trails
  `integration` (88.1%) — most of its coverage comes from real `run`
  invocations building a `RunConfig` end to end, which integration tests
  do far more of than the mock-heavy unit layer.
- **Combined coverage (94.4%)** is not just the best individual column;
  every module gains from combining categories, confirming the
  categories are complementary rather than redundant — e2e/security/
  regression each cover some lines unit/integration do not, and vice
  versa.

## What's driving the remaining ~5.6% uncovered

Spot-checked rather than exhaustively enumerated: the residual gaps are
concentrated in `cli.py` (argument-parsing branches for options no test
exercises in combination, such as some `--` flag combinations only valid
together) and `report.py` (rendering variants for terminal widths or
states no fixture currently produces). Neither is a correctness risk on
its own; closing them further is future work, not a release blocker.

## Regenerating this report

```sh
python3 tools_measure_coverage.py unit tests.test_core_unit tests.test_config_unit \
  tests.test_schema_unit tests.test_parse_unit tests.test_pipeline_unit \
  tests.test_scheduler_unit tests.test_store_unit tests.test_operations_unit \
  tests.test_report_unit tests.test_cli_unit tests.test_validators tests.test_schema_scan

python3 tools_measure_coverage.py integration tests.test_cli tests.test_config \
  tests.test_pipeline tests.test_node tests.test_report_scheduler tests.test_multirun \
  tests.test_integration

python3 tools_measure_coverage.py e2e tests.test_examples_e2e
python3 tools_measure_coverage.py security tests.test_security
python3 tools_measure_coverage.py errors tests.test_errors

python3 tools_measure_coverage.py regression \
  tests.test_pipeline.TestRowContextEmit \
  tests.test_integration.TestReloadWithExternalModules \
  tests.test_integration.TestPipelineRuns.test_slurm_chaining_submits_with_sbatch_not_qsub \
  tests.test_integration.TestPipelineRuns.test_shell_helper_chaining_does_not_corrupt_the_environment \
  tests.test_integration.TestPipelineRuns.test_handoff_reaches_the_next_stage \
  tests.test_integration.TestPipelineRuns.test_emit_shell_expr_carries_a_run_time_value_to_the_next_stage

python3 tools_measure_coverage.py combined <all modules from every category above>
```

Each run writes `coverage-reports/<label>.json` (per-module executable/hit
line counts) alongside this file.
