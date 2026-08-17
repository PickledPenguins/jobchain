# Changelog

## Unreleased

### Changed

- `jobchain.pipeline.Bool` (a stage-setting type) is renamed to
  `jobchain.pipeline.StageBool`, so it no longer shares a bare name with the
  unrelated `jobchain.schema.Bool` (a field validator) — the two classes
  used to collide under the same name in their own modules, disambiguated
  only by the alias `jobchain.__init__` applied on import. `Bool` remains
  importable from `jobchain.pipeline` as a deprecated alias for
  `StageBool`; the public `jobchain.StageBool` name is unchanged.

## 0.6 — version unification and a branch-wide cleanup pass

- Collapsed the scattered `0.5`, `0.5-v4b`, `0.5v5c` version identifiers
  (Python package, C helper, shell helper, README) to a single `0.6`,
  closing the "version and protocol identity are duplicated" finding in
  `review-docs/jobchain-maintainability-review-v4b.md`.

### Fixed

- Command injection: a `command:` stage's `{row.<column>}` placeholder is now
  expanded as a `$JC_<column>` shell-variable reference (the value reaching
  the script only through the row's already-quoted `env` file) instead of
  having the raw row value substituted directly into the generated shell
  text. See `BUGFIXES.md` item 10's "Update (0.6)".
- `store.resolve_row("column=value", ...)` could never find a row that
  failed schema validation: the lookup always returned `""` for such rows
  regardless of the actual column value, so `jobchain show --row
  column=value` on an invalid row raised "no row has column=value" even
  when the row existed. It now locates the value by the column's position
  in the schema's field order, the same way `operations._identifier_for`
  already did.
- The compiled node helper's `jc_submit_row` built its scheduler-submission
  shell command by interpolating `home`/`row`/`run_dir`/`script` into
  single-quoted `popen()` text without escaping embedded single quotes,
  unlike the shell helper (which passes the same values as safe argv
  words). A new `jc_shell_quote` closes the gap.
- The shell node helper's directory/file creation (`mkdir`, `write_atomic`,
  the claim marker, the handoff file, `events.log`) relied on the caller's
  process umask, unlike the compiled helper's explicit `0755`/`0644`; under
  a permissive umask (e.g. `000` or `002`, seen on some batch/service
  accounts) this could leave run state group- or world-writable. The shell
  helper now `chmod`s explicitly after each creation, matching the compiled
  helper's permissions regardless of umask.
- `tests/helpers.py`'s scheduler-stub `qsub`/`sbatch` backgrounded a
  submitted job with `&` without detaching its own stdin/stdout/stderr, so
  a caller reading the stub's output via a pipe (e.g. Python's
  `subprocess.run(capture_output=True)`) blocked until the backgrounded job
  finished — the opposite of the asynchronous behavior the stub exists to
  simulate. `tests/test_scheduler_fixture.py`'s async-submission test was
  silently measuring this every run; fixed by redirecting the background
  subshell's stdio to `/dev/null`.
- An unconfigured `MagicMock` passed as `store` through `cli.cmd_run` (in
  two `tests/test_cli_unit.py` tests that didn't patch `configure_logging`)
  caused `os.fspath()`'s mock fallback to produce a syntactically valid but
  fake path (`MagicMock/store.log_path/<id>`), which
  `jobchain/core.py`'s `configure_logging` then `os.makedirs`'d for real at
  the repository root on every affected test run. 108 such files had
  already been committed to git.

### Repository hygiene

- Deleted `tests/unit/`, an orphaned duplicate test tree (23 files, ~4,700
  lines) superseded by `tests/test_*_unit.py` per the `0.5-v4b` entry above
  but never removed; it was invisible to `run_tests.sh`/`Makefile` but
  still linted, accounting for roughly 40% of the repository's ruff
  warnings. `tools/measure_coverage.py`'s stale references to it were
  updated to the current module names.
- Deleted the committed `MagicMock/` debris (see above) and
  `tools_measure_coverage.py`, an orphaned root-level duplicate of
  `tools/measure_coverage.py`.
- Untracked the compiled `bin/jobchain-node` binary and generated
  `coverage-reports/`/`coverage-unit.json`; `.gitignore` now covers build
  and coverage artifacts so they cannot be accidentally committed again.
- `run_tests.sh`'s mypy invocation now also passes
  `--disable-error-code=import-untyped`, needed because newer mypy no
  longer treats PyYAML's missing stubs as covered by
  `--ignore-missing-imports` alone.
- Resolved the repository's ~1,050 ruff findings (mechanical reformatting
  via `black` for compound one-line statements, verified AST-identical
  before/after; the rest fixed individually, each verified against the
  actual code path it tests) down to zero, and fixed the test-fixture bugs
  a few of them were masking (an `assertRaises(Exception)` too broad to
  notice a mock was missing a required attribute; two redundant imports of
  the same exception class).
- Fixed a missing-fixture bug that broke `tests.test_example_matrix`,
  `tests.test_examples`, and `tests.test_integration_exhaustive` on a clean
  checkout: `examples/02_validation/`, `examples/07_complex/`, and
  `examples/08_validator_matrix/` reference `outputs/`/`results/`
  directories that git does not track when empty and that were never
  given a placeholder.

All notable changes to jobchain are recorded here. Versions increment the
lowest component.

## 0.5v5c — fault injection

- Added the dedicated `fault_injection_tests/` category.
- Added failure-injection coverage for atomic filesystem writes, scheduler submission/query failures, helper failures, malformed helper output, and corrupt state.
- Atomic text/script writers now remove abandoned temporary files after write/rename failures while preserving the previous target on failed replacement.
- Scheduler submission timeouts now become explicit `SchedulerError` failures instead of escaping as raw `TimeoutExpired`.

## 0.5-v4b

Bugfix build on top of 0.5-v3b, plus a substantial expansion of unit-level
test coverage: 548 mock-heavy tests covering every jobchain/ module,
integrated from an independently developed coverage pass and consolidated
into this project's one-file-per-subsystem convention (10 new
`tests/test_*_unit.py` files). Four tests in the incoming set caught
genuine defects or asserted behavior this codebase does not have; three
were corrected to match this codebase's actual, intentional design, and
one pointed at a real, fixed bug. See `BUGFIXES.md` for the bug and
`tests/test_core_unit.py`, `tests/test_store_unit.py`,
`tests/test_operations_unit.py` for the corrected assertions (each noted
inline).

### Fixed

- `configure_logging()` never closed a file handler it was replacing, so
  every reconfiguration after the first leaked an open file descriptor.
  See `BUGFIXES.md` item 5.

### Added

- 548 new unit tests across `tests/test_{core,config,schema,parse,
  pipeline,scheduler,store,operations,report,cli}_unit.py`, complementing
  the existing integration-style suite with focused, mock-based coverage
  of individual functions and branches.

## 0.5-v3b

Bugfix build on top of 0.5-v2b. A significant, silent correctness defect
in the portable shell node helper: chaining more rows than the configured
width through `bin/jobchain-node.sh` corrupted the handoff data of every
row past the first `width` chains, because the helper's own submission
command concatenated a flag and its value into one shell word instead of
passing them as the two separate words `qsub` (and every real scheduler)
expects. Found while building an example specifically meant to prove the
shell and compiled node helpers are interchangeable -- they were not. See
`BUGFIXES.md` for the full writeup.

### Fixed

- The shell node helper's self-chained submissions silently dropped their
  environment (`JC_HOME`/`JC_ROW`/`JC_RUN`), so a claimed row ran under
  the previous row's identity and its handoff data landed in the wrong
  row's file. Affected every pipeline run through `bin/jobchain-node.sh`
  with more rows than `width`; the compiled helper and Python's own
  initial submission were unaffected. See `BUGFIXES.md` item 4.

## 0.5-v2b

Bugfix build on top of 0.5-v1b. A significant defect: `scheduler: slurm`
worked for a run's first job submission but failed at the first
self-chained submission (every stage after the first, and every row after
the first chain), which always used `qsub` regardless of configuration.
Two distinct causes, both in the same code path: neither node helper
learned which scheduler a run used at the one point it mattered, and once
that was fixed, the compiled helper's shared submission command turned out
to format the Slurm export flag incorrectly, silently dropping every
chained job's environment variables. See `BUGFIXES.md` for the full
writeup, including how the regression test caught both causes in sequence.

### Fixed

- `scheduler: slurm` self-chained submissions used `qsub` instead of
  `sbatch`, or (after an intermediate fix) submitted correctly but lost
  their environment variables, leaving every row after the first stuck
  and never marked. Both compute-node helpers, and the C helper's build
  configuration, were affected; see `BUGFIXES.md` item 3.
- A duplicated 18-line block of preprocessor macros in
  `src/jobchain-node.c`, left over from the change above, removed.

## 0.5-v1b

Bugfix build on top of 0.5. No new features; two defects fixed, both found
while building an expanded set of end-to-end examples and verifying them
against real runs rather than exit codes alone. See `BUGFIXES.md` for the
full writeup of each, including reproduction steps and the regression tests
added.

### Fixed

- An inline schema's `validator_class:` was not made absolute when a run's
  configuration was captured to `config.final.yaml`, so `status`, `show`,
  `rerun`, `cancel`, `doctor`, `logs`, and `export` all failed with
  `SchemaError: module not found` after the first successful `run`.
  `pipeline.stage_module:` already had this handling; the schema side did
  not.
- `RowContext.emit()` always single-quoted its value in the generated
  script, so a shell variable reference passed to it (the natural way to
  publish a value only known once the script runs) was never expanded --
  the variable's name was published, not its contents. This affected the
  project's own shipped `examples/pipeline/stages.py`. A new method,
  `emit_shell_expr()`, is now the documented way to publish a run-time
  value; `emit()` remains for a literal value already known when the
  script is generated, and now also correctly escapes an embedded single
  quote in that literal, which it did not before.

## 0.5

Multi-stage pipelines. One row may now produce an ordered series of dependent
jobs rather than a single job.

### Added

**Pipelines**
- A row may run N stages, submitted together and chained by the scheduler's
  own dependency mechanism (`afterok`, `afterany`, `afternotok`).
- One submit script per stage per row, generated ahead of time. No script
  references another job, so any stage can be resubmitted by hand later
  against the data a previous run left behind.
- `JobStage` classes generate those scripts. Instances are frozen after
  construction, which makes generating across a thread pool safe by
  construction rather than by convention.
- Stage classes are resolved from the stage name, or named explicitly with
  `uses`, so two stages may share an implementation with different
  configuration and renaming a stage never breaks code.
- Stages declare the configuration keys they accept, so a typo in the
  pipeline YAML is caught at load time.
- Resources merge from pipeline defaults, the stage block, and the class, in
  that order; both YAML and class specifying a key is expected rather than a
  conflict.
- Untyped handoff values pass between stages of a row, published with
  `jobchain-node emit` and sourced by later stages. Values are carried
  forward into a new generation as a seed.

**Configuration**
- One configuration file describes a whole run: parameters, schema, pipeline,
  width, logging, and paths. The schema and pipeline may be inline or
  referenced by path. There are no `--schema` or `--pipeline` options.
- `config.original.yaml` and `config.final.yaml` are captured per run; the
  second is complete and runnable, so a run can be reproduced from disk.
- Path templates expand `{row.<column>}`, `{row.name}`, `{row.generation}`,
  `{run.name}`, `{run.home}`, and `{date}`/`{time}`/`{user}`.

**Run isolation**
- Every execution owns `.jobchain/<run name>/`. Two runs in one directory
  never interact: separate state, logs, locks, claiming, and job names.
- With several runs and no selection, commands list them and stop rather than
  guessing.
- `status --all` and `doctor --all` report across runs.

**Validation**
- Fields may be marked `unique`, which both enforces uniqueness and makes the
  column usable to name a row: `--row case_name=somecase`. `id_field` implies
  it.
- Schemas may be written as a Python class instead of YAML.
- Validation is permissive by default: invalid rows are recorded, skipped, and
  reported prominently. `strict: true` restores the old behaviour.
- Invalid rows keep state but get no scripts, so they can be corrected into a
  run that is already going.

**Operating**
- Command surface reduced to eight: `run`, `status`, `show`, `rerun`,
  `cancel`, `doctor`, `logs`, `export`.
- `run` is state-aware: repeating it does whatever remains.
- `status` always prints a table, `show` always prints sections.
- `cancel --stop` takes no new work; `cancel --all` also stops the chain;
  `run --resume` clears it.
- Completion detection: `done.json`, `completions.log`, and an `on_complete`
  hook.
- Tiered confirmation before re-running a completed row, based on whether its
  output directories still exist.
- Parallel script generation with a progress bar.
- A shell implementation of the node helper, for sites that cannot compile.

### Changed

- The scheduler is configured, never detected: a wrong guess produces scripts
  whose directives the other scheduler silently ignores.
- `format.header` defaults to false.
- There is no default attempt cap.
- Messages carry no hint text and no suggested commands. Commands are
  documented once, in the help.
- Output is reported by directory with counts, never as a file listing.

### Removed

- `init`, `start`, `validate`, `explain`, `plan`, `metrics`, `retry`,
  `revise`, `reset`, and `set`, all folded into the eight remaining commands.
- `--collapse-delimiters`, `confirm_threshold`, `--node-binary`.
- The `job` section of a schema; resources belong to the pipeline.

### Fixed

Found by auditing the implementation against the design document, which
compared every documented option, YAML key, ordering rule, and combination
rule against the code:

- `max_in_flight` was accepted and stored but never enforced. It now caps the
  number of pipelines submitted but unfinished, so a fast first stage cannot
  queue an entire parameter file while later stages are still running.
- `status --all --prune-after DAYS` was documented but absent. It now lists
  runs that finished longer ago than the given number of days and removes them
  only with `--yes`.

Bugs found by the test suite while building this release:

- Every row wrote its scripts to the same directory when the work directory
  template used `{row.name}`, because a stage could not know the row's name.
  Stages now receive the context.
- A submitter could overwrite a status the job had already written, since
  `qsub` returns before the job starts. Recording a job id and recording a
  status are now separate operations.
- Seeding the handoff for a new generation created that generation's
  directory, which is the claim marker, leaving the row permanently
  unclaimable. The seed now lives beside the row.
- A row that failed validation lost its identifier and its other column
  values, so it could be neither found nor corrected. Raw fields are kept.
- Scripts baked in their generation's run directory, so a rerun wrote status
  into the previous attempt. Scripts now honour the run directory passed at
  submission.
- The completion marker survived a rerun that finished quickly. It is now
  cleared the moment a row is re-queued.

Alongside the `0.5-v1b` through `0.5-v4b` bugfix builds above, a separate
track of releases added whole test categories rather than fixing defects.
The exact interleaving between the two tracks is not reconstructable from
this record; they are grouped here by track instead, newest first within
each, and both precede the `0.6` version-identifier unification.

## 0.5v7c

- Added dedicated `bottleneck_tests` for architecture-specific scaling risks.
- Added discovery-scan, claim-hotspot, reporting, scheduler-backpressure, and width-profile tests.
- Added `make bottlenecks` and integrated the category into `run_tests.sh`.

## 0.5v6c (inferred label — see note)

A dedicated `load_tests/` category now exercises larger state sets and
process contention with bounded, reproducible workloads: a 5,000-row state
load, a 2,000-row claim workload with 24 workers, and repeated medium loads,
to catch catastrophic performance regressions and throughput collapse. The
tests are regression guards, not hardware-specific benchmarks. This entry
was found misplaced in `BUGFIXES.md` (not itself a bug fix) with no version
label of its own; `0.5v6c` is inferred from the otherwise-unexplained gap
between `0.5v4c` and `0.5v7c` in this track's numbering, not confirmed.

## 0.5v4c

### Testing: Concurrency & Race Testing

- Added `concurrency_tests/` as a dedicated testing category.
- Added real multiprocessing contention tests for the Python claim wrapper.
- Added setup-lock ownership tests proving one owner while the lock is held.
- Added stop/claim quiescence testing and generation-isolation contention tests.
- Integrated the category into `run_tests.sh` and `make concurrency`.

## 0.5v3c

- Added dedicated State & Property Testing category.
- Added deterministic generated state/property checks and Make target.

## Mutation testing category

Added a dedicated `mutation_tests/` category with a dependency-free mutation
runner. The initial 9 semantic mutants cover store state roll-ups, terminal
state detection, operation force/continuation decisions, and scheduler result
and timeout handling. Baseline mutation score: 9/9 killed (100%).

## 0.4

Condensed the source layout: the C helper became one translation unit, and the
Python package went from twelve modules to eight.

## 0.3

Documentation completeness: scope, project structure, configuration, pipeline
order, and per-option edge-case behavior.

## 0.2

Reduced the command surface from sixteen commands to thirteen.

## 0.1

First release: schema-driven validation, lock-free claiming, self-chaining
execution for PBS Professional and Slurm, mid-run correction, and
reconciliation.
