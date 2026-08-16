# Proposed Test Isolation Architecture — 0.5v5c.3

## Objective

Make the Jobchain test system deterministic, resource-safe, order-independent, and diagnosable without sacrificing the existing process-isolated execution speed.

The proposal deliberately separates two goals:

1. **Production test execution:** isolate tests so one failure cannot contaminate another.
2. **Test cleanliness verification:** deliberately remove that isolation and verify that the tests themselves clean up correctly.

The second mode is an audit tool, not the normal release gate.

## Recommended architecture

```text
                         Test command
                              |
              +---------------+----------------+
              |                                |
        Normal test gate                 Isolation audit
              |                                |
      module process isolation          one interpreter
              |                                |
      one module / process             module-by-module
              |                                |
      process-group ownership           snapshot global state
              |                                |
      per-module timeout               detect leaked state
              |                                |
      coverage collection              detect leaked resources
              |                                |
              +---------------+----------------+
                              |
                    unified test report
```

## 1. Keep process isolation as the normal execution model

`tests/run_suite.py` should remain the normal Python test runner.

Each test module should continue to run in a fresh interpreter because this provides a strong containment boundary around:

- imported module state;
- environment variables;
- logging configuration;
- current directory;
- stdin/stdout/stderr replacement;
- threads;
- subprocesses;
- third-party library state.

This is particularly appropriate for Jobchain because many integration tests intentionally create scheduler processes and execute generated shell scripts.

### Justification

Removing this isolation merely to make the suite "pure unittest" would make the suite less reliable. Process isolation is a feature, not a workaround.

## 2. Add a dedicated serial isolation-audit mode

Add a test-only mode that loads all test modules into one interpreter and runs them in deterministic order.

The audit runner should:

1. record process-global state before each module;
2. run the module;
3. record the same state afterward;
4. report differences;
5. continue to the next module where safe;
6. fail the audit if a module leaks state.

The state snapshot should include at least:

```text
current working directory
selected environment variables / complete environment
sys.path
sys.stdin / sys.stdout / sys.stderr identity
Jobchain logger handlers
Jobchain logger level
Jobchain logger propagation setting
active Python threads
owned child processes
registered temporary resources
```

### Justification

The normal process boundary hides many test leaks. The audit mode makes those leaks visible without weakening the normal release gate.

## 3. Introduce one authoritative test-environment fixture

Extend `tests/helpers.py` with one reusable isolation fixture/context manager responsible for:

- environment snapshot/restore;
- cwd snapshot/restore;
- standard-stream restoration;
- Jobchain logger snapshot/restore;
- cleanup registration;
- child-process ownership.

Tests should not implement ad-hoc global-state restoration when the shared fixture can provide it.

Conceptually:

```text
TestEnvironment
    snapshot()
    set_env(...)
    chdir(...)
    replace_stdin(...)
    track_process(...)
    add_cleanup(...)
    assert_clean()
    restore()
```

### Justification

Centralized cleanup eliminates several subtly different cleanup implementations and makes future global-state additions much safer.

## 4. Give scheduler stub processes explicit ownership

The scheduler stub should return or register the PID of every background job it starts.

`TempProject` should own those processes and clean them up in `addCleanup()`/`tearDown()`.

The desired lifecycle is:

```text
install scheduler
      |
create child
      |
register child PID
      |
test waits for completion
      |
assert child exited
      |
cleanup verifies no owned child remains
```

`wait_for_jobs()` should continue to use the state files as a behavioral assertion, but state-file disappearance should not be treated as proof that the operating-system process is gone.

### Justification

A test framework should own the processes it creates. This eliminates an important class of hangs and false quiescence.

## 5. Make resource cleanup fail loudly

At the end of every integration test environment, cleanup should verify:

- no tracked scheduler child remains;
- no test-owned temporary resource remains unexpectedly;
- cwd has been restored;
- environment has been restored;
- stdin/stdout/stderr have been restored;
- Jobchain logger handlers match their initial state;
- no unexpected Jobchain worker thread remains.

Cleanup failures should be reported as test failures, not merely warnings.

### Justification

A leaked resource is itself a test failure because it makes subsequent tests unreliable.

## 6. Make logger state transactional

Rather than clearing handlers in individual test classes, the fixture should snapshot:

```text
handlers
level
propagate
filters
```

and restore them exactly after each test or test class, closing handlers created by the test.

This should also eliminate the current `ResourceWarning` caused by an unclosed log file.

### Justification

Logging is a process-global singleton. Exact restoration is safer than assuming that removing handlers is sufficient.

## 7. Replace ad-hoc cwd changes with a context manager

Provide a helper such as:

```text
with temporary_cwd(path):
    ...
```

All tests needing a different cwd should use it.

### Justification

This converts cwd restoration from a convention into a guaranteed cleanup boundary.

## 8. Replace direct environment mutation with a scoped helper

Provide a helper such as:

```text
with temporary_environment(PATH=..., JOBCHAIN_NODE=...):
    ...
```

Existing `TempProject` environment restoration can remain as a final safety net.

### Justification

Nested environment changes become composable and cannot accidentally overwrite another test's restoration state.

## 9. Add explicit test tiers

The test architecture should expose these tiers:

| Tier | Purpose | Normal release gate |
|---|---|---|
| Fast | Unit + smoke | Yes |
| Full | All Python module tests + integration | Yes |
| Isolation audit | Same-interpreter cleanliness | Yes for production-ready release |
| Sanitizer | C memory/UB behavior | Production-ready gate |
| C coverage | C source coverage | Production-ready gate |
| Fault injection | Failure recovery | Production-ready gate |
| Concurrency | Race behavior | Production-ready gate |
| Mutation | Assertion strength | Extended |
| Real scheduler | PBS/Slurm environment | Environment-dependent |
| Load/stress | Scalability | Explicit |

### Justification

The project already has most of these categories. Formalizing the tiers makes it clear which checks are expected for routine development and which are deliberately expensive.

## 10. Keep coverage collection independent of isolation auditing

Normal Python coverage should continue to use per-process coverage files and `coverage combine`.

The isolation audit should not become the primary coverage source.

### Justification

Coverage correctness and test isolation answer different questions. Combining them would make failures harder to diagnose and could encourage running the least reliable execution mode merely to obtain coverage.

## 11. Add a deterministic order matrix to the audit

The isolation audit should support:

```text
alphabetical order
reverse order
explicitly known-risk order
```

The normal release audit should use alphabetical order for reproducibility. A periodic extended check can run reverse order as well.

### Justification

If tests only work in one order, the audit should expose that fact. Multiple orders make hidden dependencies easier to find.

## 12. Add a module-level resource report

When an audit detects contamination, report it in a form such as:

```text
MODULE: tests.test_errors

Environment changes:
  JOBCHAIN_NODE: <unset> -> /path/to/node

Logger changes:
  handler count: 0 -> 1
  level: INFO -> DEBUG

Threads:
  new: Thread-7

Children:
  PID 12345 /bin/sh ...
```

The report should identify the module that introduced the difference rather than only reporting that the final suite is dirty.

### Justification

A maintainer needs the first contaminated boundary to fix a leak. A final-suite snapshot is insufficient.

## 13. Do not fix contamination by imposing test order

The following should explicitly be avoided:

```text
run test A before test B
skip test B after test A
special-case module order
sleep to allow children to exit
increase timeouts until the suite passes
```

### Justification

These approaches conceal resource ownership problems instead of solving them.

## 14. Migration sequence

### Phase A — infrastructure

- Add the scoped environment/cwd/stdin/logger helpers.
- Add child-process ownership to `TempProject`.
- Make cleanup assertions explicit.

### Phase B — convert existing tests

- Convert direct environment mutation.
- Convert direct cwd changes.
- Remove duplicated logger cleanup.
- Register scheduler children.

### Phase C — audit runner

- Add deterministic same-interpreter execution.
- Add global-state snapshots.
- Add resource-leak reporting.
- Run alphabetical and reverse-order audits.

### Phase D — release integration

- Keep isolated modules as the standard fast/full runner.
- Require the isolation audit for production-ready releases.
- Preserve longer mutation/load/real-scheduler tests as extended tiers.

## Expected result

The resulting architecture should provide both properties that are currently missing:

```text
Normal execution:
    a bad test cannot contaminate unrelated tests.

Audit execution:
    a test cannot silently rely on process isolation to hide its leak.
```

This is preferable to replacing process isolation with a single shared interpreter. Jobchain's scheduler and subprocess-heavy integration surface benefits from strong process containment, while the audit mode provides the missing proof that the tests are intrinsically clean.

## Decision recommendation

Adopt the architecture above.

The first implementation should be limited to testing files and should not change Jobchain production behavior. The highest-value first implementation targets are the scoped test environment, explicit scheduler-child ownership, and the serial isolation-audit runner. Only after those are in place should individual test cases be converted.
