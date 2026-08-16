# Jobchain Test Surface Matrix

This document is the testing architecture inventory for the `0.5v5c.2`
baseline. It records where behavior is tested and identifies areas that are
only partially covered or intentionally deferred.

Legend:

- **Strong** — dedicated tests exist at the indicated layer.
- **Partial** — behavior is exercised, but an important dimension is missing.
- **Gap** — no dedicated test surface was identified.
- **N/A** — the layer is not appropriate for that component.

## Production-module matrix

| Production component | Unit | Integration | E2E | Property | Fault | Concurrency | Security | C/Shell | Notes |
|---|---|---|---|---|---|---|---|---|---|
| `core.py` | Strong | Partial | Strong | Partial | Partial | N/A | Partial | N/A | Exceptions, logging, exit behavior; crash/recovery invariants remain limited. |
| `schema.py` | Strong | Strong | Partial | Gap | Partial | N/A | Partial | N/A | Validator and loader surface is broad; malformed/fuzzed schema generation is missing. |
| `parse.py` | Strong | Strong | Partial | Gap | Partial | N/A | Partial | N/A | Parser branches are well exercised; property/fuzz testing is missing. |
| `config.py` | Strong | Strong | Strong | Gap | Partial | N/A | Partial | N/A | Configuration combinations are not exhaustively modeled as a matrix. |
| `pipeline.py` | Strong | Strong | Strong | Gap | Partial | Partial | Partial | N/A | Stage construction and chaining are covered; larger graph/property space remains. |
| `store.py` | Strong | Strong | Partial | Strong | Strong | Strong | Partial | Partial | One of the strongest areas; crash consistency and shared-filesystem behavior remain gaps. |
| `scheduler.py` | Strong | Strong | Strong | Gap | Strong | Partial | Partial | Partial | Fake scheduler is extensive; real PBS/Slurm integration is optional/missing as a release gate. |
| `operations.py` | Strong | Strong | Strong | Partial | Strong | Partial | Partial | N/A | Major command paths are covered; distributed crash boundaries need dedicated tests. |
| `report.py` | Strong | Strong | Strong | Gap | Partial | N/A | Gap | N/A | Rendering and metrics are covered; large-scale/report robustness is not. |
| `cli.py` | Strong | Strong | Strong | Gap | Partial | N/A | Partial | Partial | Command/options surface is broad; option interaction matrix is incomplete. |
| `jobchain-node.c` | Partial | Strong | Strong | Gap | Strong | Strong | Partial | Strong | Sanitizer and gcov gates exist; C source coverage is measured separately. |
| `bin/jobchain` | N/A | Strong | Strong | N/A | Partial | N/A | Partial | Strong | Behavioral shell testing is indirect; dedicated shell tests remain limited. |
| `bin/jobchain-node.sh` | N/A | Strong | Strong | N/A | Partial | Strong | Partial | Strong | Syntax and integration behavior covered; shell-specific failure matrix is incomplete. |

## CLI command matrix

| Command | Happy path | Invalid input | Existing state | JSON | Scheduler interaction | Recovery/idempotency |
|---|---|---|---|---|---|---|
| `run` | Strong | Strong | Strong | Strong | Strong | Partial |
| `status` | Strong | Partial | Strong | Strong | Strong | Partial |
| `show` | Strong | Strong | Strong | Partial | Strong | Partial |
| `rerun` | Strong | Strong | Strong | Strong | Strong | Partial |
| `cancel` | Strong | Strong | Strong | Strong | Strong | Partial |
| `doctor` | Strong | Partial | Strong | Strong | Strong | Partial |
| `logs` | Strong | Partial | Strong | Strong | Partial | Gap |
| `export` | Strong | Partial | Strong | Strong | Partial | Gap |

## Cross-cutting behavior matrix

| Behavior | Current surface | Assessment |
|---|---|---|
| Input validation | Unit + integration + CLI | Strong |
| Parser normalization | Unit + integration | Strong |
| Configuration defaults/merge | Unit + integration | Strong |
| Pipeline construction | Unit + integration + E2E | Strong |
| Scheduler submission/query/cancel | Unit + fake-scheduler integration | Strong with real-scheduler gap |
| State transitions | Unit + property + concurrency | Strong |
| Generation handling | Unit + property + concurrency | Strong |
| Atomic writes | Unit + fault injection | Strong |
| Malformed persisted state | Unit + fault injection | Strong |
| Concurrent claims | Concurrency | Strong for local filesystem |
| Concurrent stop/claim interactions | Partial | Needs expansion |
| Process crash during state transition | Partial | Dedicated crash-consistency tests needed |
| Scheduler accepts job then local process dies | Gap | High priority |
| Scheduler response lost after submission | Partial | Needs end-to-end state invariant |
| Operation idempotency | Partial | Needs systematic command matrix |
| Configuration option interactions | Partial | Needs decision-table/property coverage |
| Fuzz/property testing of parsers | Gap | High-value future work |
| Large-run scalability | Gap | Needs load/stress tier |
| Shared/network filesystem semantics | Gap | Environment-dependent integration needed |
| Upgrade compatibility | Gap | Persisted-state fixture suite needed |
| Deterministic artifact generation | Partial | Needs explicit reproducibility assertions |
| Security requirements | Partial | Known unsafe behaviors are explicit expected failures |
| C line coverage | Strong | Threshold enforced |
| C branch coverage | Strong | Threshold enforced |
| Shell syntax | Strong | `sh -n` gate |
| Shell behavioral coverage | Partial | Mostly indirect through integration tests |

## Test execution tiers

| Tier | Intended contents | Release-gate status |
|---|---|---|
| Fast | Python unit/CLI tests, static checks, short smoke tests | Required |
| Full | Fast + integration + property + concurrency + fault injection | Required |
| Extended | Full + C coverage + sanitizer tests | Required for production-ready release |
| Mutation | Extended + mutation suite | Required before declaring test architecture mature |
| Load | Large datasets, long polling, high contention | Explicitly invoked |
| Real scheduler | PBS/Slurm integration | Environment-dependent |

## Current verification snapshot

The complete `run_tests.sh` invocation was attempted. The suite exposed two
important categories of test-infrastructure behavior rather than simply
returning a clean aggregate result:

1. The CLI and integration modules contain order-sensitive execution paths.
   Their classes pass when isolated, but the complete module can run much
   longer because one test leaves state/process behavior that affects later
   tests. This is precisely the type of contamination the process-isolated
   runner is intended to detect.
2. `test_errors.py` previously required every Python file under `tests/` to
   appear in the user-facing README structure tree. Internal testing helpers
   (`tests/run_suite.py` and `tests/test_coverage_gaps.py`) are not user-facing
   project components, so that assertion has been narrowed to production
   source documentation requirements.

Individual execution of all previously problematic CLI and integration
classes was successful after isolation. The remaining full-suite execution
problem should be treated as a test-isolation/reproducibility issue, not
hidden by excluding the affected modules.

## High-priority gaps identified by this matrix

1. Crash consistency around scheduler submission and local state recording.
2. Systematic idempotency testing for every state-changing command.
3. Configuration interaction/decision-table coverage.
4. Property/fuzz testing for parsers and schema/configuration loaders.
5. Expanded concurrency interleavings.
6. Large-run scalability/load testing.
7. Shared-filesystem integration testing.
8. Persisted-state upgrade compatibility.
9. Explicit deterministic-output/reproducibility testing.
10. Real PBS/Slurm integration testing where those schedulers are available.
