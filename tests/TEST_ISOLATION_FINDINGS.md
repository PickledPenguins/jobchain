# Test Isolation Investigation — 0.5v5c.3

## Purpose

This document records the results of the first two steps of the test-isolation investigation:

1. Reproduce the suspected complete-suite/order-dependent behavior from `0.5v5c.2`.
2. Identify test-side global state and process resources that can cross test boundaries.

No production code or test behavior was changed as part of this investigation. The companion `TEST_ARCHITECTURE_PROPOSAL.md` proposes the architecture for the eventual fix.

## 1. Reproduction results

The supplied test tree was built from a clean extraction of `0.5v5c.2`.

### Individual module results

The following modules were executed independently in a fresh Python interpreter and passed:

| Module | Result | Observed runtime |
|---|---|---:|
| `tests.test_cli` | PASS | 65 tests / 27.325 s |
| `tests.test_config` | PASS | 33 / 0.032 s |
| `tests.test_config_unit` | PASS | 20 / 0.012 s |
| `tests.test_core_unit` | PASS | 12 / 0.010 s |
| `tests.test_coverage_gaps` | PASS | 15 / 0.016 s |
| `tests.test_errors` | PASS | 34 / 0.557 s |
| `tests.test_examples_e2e` | PASS | 8 / 3.772 s |
| `tests.test_integration` | PASS | 63 / 26.689 s |

The important observation is that the CLI and integration modules pass when independently executed.

### Same-interpreter discovery

A conventional single-interpreter discovery run was also attempted:

```text
python3 -m unittest discover -s tests -p 'test_*.py'
```

It did not complete within a 360-second external execution limit. A verbose run consistently progressed through the early modules and entered the integration suite, whereas the corresponding modules completed when executed independently.

This establishes a meaningful difference between:

```text
fresh interpreter per module
```

and:

```text
all modules in one interpreter
```

It is therefore not safe to treat the existing passing isolated-module results as proof that the suite is order-independent.

### Pairwise reproduction

`tests.test_errors` followed by `tests.test_integration` completed successfully in one interpreter:

```text
97 tests / 25.963 s / OK
```

`tests.test_cli` followed by selected integration classes also demonstrated inconsistent completion behavior during repeated investigation runs. Some executions completed all selected tests but did not terminate cleanly under the external command timeout; other executions progressed much more slowly or stopped before the final result.

This behavior is sufficiently nondeterministic that it should be treated as a test-infrastructure defect rather than normalized as a fixed test-order requirement.

## 2. Directly identified shared-state surfaces

The repository contains several process-global resources that can cross test boundaries when tests share an interpreter.

### Environment variables

The following tests directly mutate `os.environ`:

- `tests/test_integration.py`
- `tests/test_report_scheduler.py`
- `tests/test_multirun.py`
- `tests/test_validators.py`
- `tests/test_security.py`
- `tests/test_examples_e2e.py`
- `tests/test_errors.py`
- `tests/helpers.py`

`TempProject` snapshots and restores the complete environment, which protects tests deriving from it. However, environment mutation remains an architectural hazard because individual tests can modify global state before cleanup and not every test class is based on the same fixture.

The most important variables are:

- `PATH`
- `JOBCHAIN_NODE`
- `JOBCHAIN_RUN`
- `JOBCHAIN_TEST_DIR`

`PATH` is particularly significant because scheduler discovery depends on it.

### Current working directory

Several tests explicitly call `os.chdir()` and restore the old directory manually. `TempProject.run_cli()` also changes the process working directory around every CLI invocation.

This creates a global resource with multiple independent restoration mechanisms. A failure during an unguarded path would affect every later test in the interpreter.

### Standard input

`TempProject.run_cli()` temporarily replaces `sys.stdin` with `_Stdin` and restores it in a context manager. This is correctly scoped today, but it remains process-global and therefore belongs in a centralized test-state isolation mechanism.

### Global logging state

The production logger is a process-global `logging.Logger`. Tests directly modify:

- `logger.handlers`
- `logger.level`
- handler streams
- handler lifetime

`tests/test_core_unit.py` and `tests/test_errors.py` contain explicit cleanup, but the cleanup strategies are different. `tests/test_errors.py` clears handlers in `tearDown()` but does not restore every logger attribute that a test could change.

The test run also emitted this warning:

```text
ResourceWarning: unclosed file .../run.log
```

from `tests/test_errors.py` while clearing logging handlers. That is direct evidence that logger/resource cleanup is not uniformly complete.

### Subprocesses

The scheduler stubs in `tests/helpers.py` intentionally launch background shell jobs:

```text
( ... ) &
```

`wait_for_jobs()` infers quiescence from `running.*` files rather than tracking the actual child process IDs. This means the test framework does not have direct ownership of every process it causes to exist.

This is an important architectural weakness for reliable cleanup. A process can outlive the filesystem marker that the test uses as its completion signal, or a child can inherit a pipe/file descriptor that keeps a parent command alive.

### Threads

Production operations use `ThreadPoolExecutor`. CLI tests call production operations directly inside the test interpreter rather than spawning a separate CLI process. The executor normally shuts down through its context manager, but thread lifecycle is therefore another process-global resource that is only indirectly controlled by the test fixture.

### Module/global state

Tests import production modules into the shared interpreter. Any production module-level cache, singleton, logger, registry, or imported external module state persists across all later tests in conventional discovery.

The current suite has no general mechanism for detecting or restoring arbitrary module-level state.

## 3. Important distinction: isolation already exists, but is incomplete

`tests/run_suite.py` introduced module-level process isolation in `0.5v5c.1`. That is the correct direction and substantially reduces contamination.

However, the current architecture has two limitations:

1. It makes the normal suite isolated, but it does not prove that the tests themselves are clean when executed in a shared interpreter.
2. It does not provide systematic ownership and cleanup for background scheduler processes and other resources created by tests.

The first point matters because a test can appear reliable only because a process boundary happens to erase its leaked state.

The second matters because process isolation cannot repair a test that leaks children outside the expected process group or leaves filesystem/scheduler activity running after the test claims completion.

## 4. Findings ranked by importance

### High — No mandatory same-interpreter isolation audit

The normal runner intentionally avoids the contamination problem by isolating modules. There is currently no separate deterministic mode that proves the test modules can coexist safely in one interpreter.

### High — Scheduler stubs use unowned background processes

The stub scheduler creates background jobs but exposes no explicit child-PID ownership to the test fixture. Completion is inferred from state files.

### Medium — Logging state is manually managed in multiple places

There are several independent logger cleanup strategies, and at least one run emitted an unclosed-file warning.

### Medium — Environment restoration is fixture-dependent

`TempProject` provides strong environment restoration, but direct environment mutation remains scattered throughout the suite.

### Medium — Working-directory restoration is distributed

Several tests implement their own `old = os.getcwd(); os.chdir(...); finally: os.chdir(old)` logic instead of using one fixture/context manager.

### Medium — No generic leaked-resource detector

The suite does not currently fail a test or module because it leaves behind changed environment state, changed cwd, logger handlers, threads, or child processes.

## 5. What this investigation does not claim

The investigation does **not** claim that one specific environment variable or logger mutation is the sole cause of the long-running same-interpreter behavior. The observed behavior is affected by subprocess-based integration tests and is not yet sufficiently deterministic to attribute to one line without changing the test architecture first.

The evidence does establish that the test system has multiple global-resource boundaries that are not centrally owned, and that same-interpreter execution is materially less reliable than the isolated-module execution path.

## Conclusion

The correct next action is not to reorder test modules. The suite needs explicit resource ownership and cleanup boundaries, plus a dedicated isolation-audit mode that intentionally runs modules in one interpreter and reports leaked process-global state.

The proposed architecture is documented in `tests/TEST_ARCHITECTURE_PROPOSAL.md`.
