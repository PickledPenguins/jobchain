# Jobchain Test Coverage Campaign

## Purpose

Build the example and automated test suite into a comprehensive executable specification of jobchain. Coverage is measured separately for unit, smoke, regression, integration, end-to-end (E2E), and performance tests, followed by a combined production gate.

A test may belong to more than one category. Coverage for each category is measured by selecting tests with that category tag.

## Coverage Targets

| Category | Line | Statement | Function | Branch | Condition | Bounded Path |
|---|---:|---:|---:|---:|---:|---:|
| Unit | >=95% | >=95% | >=98% | >=90% | >=90% | >=80% |
| Smoke | >=70% | >=70% | >=80% | >=60% | >=60% | >=40% |
| Regression | >=95% | >=95% | >=98% | >=90% | >=90% | >=80% |
| Integration | >=90% | >=90% | >=95% | >=80% | >=80% | >=65% |
| E2E | >=85% | >=85% | >=90% | >=75% | >=75% | >=60% |
| Performance | >=70% | >=70% | >=80% | >=50% | >=50% | >=30% |
| Combined | >=95% | >=95% | >=98% | >=90% | >=90% | >=80% |

Path coverage is measured against explicitly defined, bounded path sets for critical functions. Literal exhaustive path coverage is not a practical requirement for a nontrivial CLI/HPC application.

## Non-percentage requirements

- Every documented feature has at least one executable example/test.
- Every public CLI command is tested.
- Every meaningful public CLI option is tested.
- Every validator is tested, including invalid and boundary behavior.
- Every supported scheduler backend is tested.
- Every dependency type is tested.
- Every resource type is tested.
- Every major state transition is tested.
- Every documented failure/recovery mechanism is tested.
- Every fixed defect has a permanent regression test.
- Concurrency and filesystem-locking protocols have race-oriented tests.
- No unexplained uncovered functions, statements, or branches remain at a phase gate.
- No known flaky tests, resource leaks, or indefinite hangs remain at a phase gate.
- Coverage exclusions are rare, explicit, and justified; coverage pragmas must not be used merely to inflate metrics.

## Execution Order

### Phase 1 — Unit

1. Establish authoritative coverage tooling and category tagging.
2. Establish a unit-only baseline.
3. Reach >=98% function coverage.
4. Reach >=95% statement and line coverage.
5. Close validator boundary and error-path gaps.
6. Close parser/input-format gaps.
7. Close configuration/default/precedence gaps.
8. Close pipeline topology and dependency gaps.
9. Close resource-resolution gaps.
10. Close store/state/generation/handoff gaps.
11. Close filesystem/locking gaps.
12. Close scheduler rendering/parsing/error gaps.
13. Close operations gaps.
14. Close branch and condition gaps.
15. Define and close bounded critical-path sets.
16. Freeze the unit gate only when all targets and non-percentage requirements pass.

### Phase 2 — Smoke

Create a small, fast suite covering startup, basic configuration, generation, execution, scheduler interaction, handoff, chaining, failure, rerun, cancellation, doctor, and reporting.

### Phase 3 — Regression

Convert all known defects into permanent executable regression scenarios. Include concurrency races, state inconsistencies, scheduler failures, generation/handoff bugs, resource leaks, and fixture failures.

### Phase 4 — Integration

Exercise component combinations: CLI/config/schema, schema/pipeline, pipeline/scheduler, store/operations, scheduler/reconciliation, handoff/generation, and failure/recovery combinations.

### Phase 5 — E2E

Build complete user workflows through validation, run creation, generation, submission, dependency execution, handoff, chaining, completion, reporting, rerun, recovery, and cancellation for supported schedulers.

### Phase 6 — Performance

Build scalable scenarios across row count, stage count, width, workers, generation count, contention, and scheduler interactions. Measure runtime, CPU, memory, filesystem behavior, and scheduler submission volume in addition to coverage.

### Phase 7 — Combined production gate

Run all applicable categories and verify >=95% line/statement, >=98% function, >=90% branch/condition, and >=80% bounded path coverage, plus all feature-coverage requirements.

## Unit Work Breakdown

### UNIT-01 — Coverage infrastructure

- Define category metadata for tests/examples.
- Add repeatable category-specific coverage commands.
- Produce line, statement, function, branch, condition, and bounded-path reports where supported.
- Produce machine-readable results.
- Record the baseline.

### UNIT-02 — Function inventory

- Enumerate every implementation function/method.
- Classify uncovered functions.
- Add focused tests for legitimate uncovered functions.
- Document justified exclusions.

### UNIT-03 — `core.py`

Close normal, boundary, error, state, and filesystem execution paths.

### UNIT-04 — `config.py`

Cover defaults, normalization, validation, templates, run names, overrides, invalid configuration, and precedence.

### UNIT-05 — `schema.py`

Cover every validator and registry path, including all boundary and failure cases.

### UNIT-06 — `parse.py`

Cover delimiters, headers, comments, blank lines, quoting, escapes, malformed rows, encodings, and normalization.

### UNIT-07 — `pipeline.py`

Cover topology construction, stage resolution, dependencies, chaining, settings, resources, and invalid configurations.

### UNIT-08 — `store.py`

Cover state transitions, generations, claims, handoff, locks, corruption/partial state, and recovery.

### UNIT-09 — `scheduler.py`

Cover PBS/Slurm command generation, dependency rendering, resource rendering, job ID parsing, status, cancellation, malformed responses, and missing executables.

### UNIT-10 — `operations.py`

Cover run/rerun/cancel/doctor/reconcile/regenerate/resume and their no-op, invalid, partial, failed, and forced states.

### UNIT-11 — Filesystem/concurrency

Cover lock acquisition/release, stale locks, simultaneous access, partial preparation, cleanup, and process-level races.

### UNIT-12 — Error/boundary matrix

Systematically cover invalid types, missing values, empty values, minimum/maximum boundaries, malformed inputs, unavailable resources, and scheduler failures.

### UNIT-13 — Branch/condition closure

Use the coverage report to enumerate every remaining branch/condition and add the smallest meaningful test for each.

### UNIT-14 — Bounded path closure

Define explicit paths for critical functions such as validation, resource resolution, state transitions, locking, scheduler submission, and rerun/handoff logic.

### UNIT-15 — Unit quality gate

Require all unit thresholds, all critical-feature requirements, and clean repeated execution before starting Smoke.

## Coverage Reporting Standard

Every coverage run should record:

- test category;
- test count;
- pass/fail/skip count;
- source revision;
- line coverage;
- statement coverage;
- function coverage;
- branch coverage;
- condition coverage when the instrumentation tool supports it;
- bounded path coverage for the defined path model;
- uncovered functions;
- uncovered lines;
- uncovered branches;
- runtime;
- resource/leak warnings.

Coverage numbers must never be used to hide failing tests. A category is passing only when both its tests and its coverage thresholds pass.

## Current Progress — Phase 1

### UNIT-01 — In progress

Implemented:

- `COVERAGE_PLAN.md` with category thresholds and staged work breakdown.
- `tools/measure_coverage.py` with explicit category test lists.
- Machine-readable JSON coverage reports under `coverage-reports/`.
- Function coverage derived from coverage.py's per-function execution data.
- Branch coverage from coverage.py's branch instrumentation.
- Explicit reporting that condition and bounded-path metrics are not yet instrumented.
- Initial focused operations-helper unit tests under `tests/unit/`.

Initial unit baseline after the first operations-helper batch:

| Metric | Current | Target |
|---|---:|---:|
| Line | 72.43% | >=95% |
| Statement | 76.09% | >=95% |
| Function | 83.25% | >=98% |
| Branch | 62.61% | >=90% |
| Condition | Not yet instrumented | >=90% |
| Bounded path | Not yet implemented | >=80% |

The unit tests themselves currently pass: 333 tests, 0 failures. The baseline demonstrates that substantial work remains, especially in `operations.py`, `cli.py`, and `store.py`. `cli.py` is intentionally not yet a primary unit target; its broad behavior will be covered in later Smoke/Integration/E2E phases, while pure argument parsing helpers can be added to the unit suite where appropriate.

Next unit batch: close the uncovered `operations.py` function/branch inventory, followed by `store.py`, `parse.py`, `pipeline.py`, and schema-loading/error paths. Do not begin Smoke until the Unit gate is satisfied.
