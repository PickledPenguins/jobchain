# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

jobchain runs scheduler (PBS/Slurm) job pipelines from a delimited parameter file. Each row is one
unit of work — a single job or an ordered series of dependent jobs. A fixed number of pipelines
("chains") advance concurrently on compute nodes; each stage submits its successor as it finishes,
so the queue stays occupied without an external driver process.

Four principles shape the whole design (see README.md "What it does" for the full rationale):
- A submit script is never tied to another job — dependencies live in scheduler arguments, not in
  the script itself, so any script can be resubmitted standalone.
- Validation happens before any stage object exists — invalid rows never reach stage code.
- The stage class owns its script completely (contents, directives, output path).
- Every execution is isolated — concurrent runs in one directory cannot collide.

## Commands

```sh
./install.sh                    # build bin/jobchain-node, check prerequisites, verify filesystem
./run_tests.sh                  # build (sanitized), run full suite, coverage, static analysis
./run_tests.sh --fast           # skip coverage and the sanitizer build
./run_tests.sh --no-sanitizer   # optimized C build instead of ASan/UBSan
./run_tests.sh --no-c-coverage  # skip the dedicated C source coverage pass
./run_tests.sh test_node.py     # run only a matching test module (each module = one interpreter)

make                             # build bin/jobchain-node (optimized)
make debug                       # same, with AddressSanitizer + UndefinedBehaviorSanitizer
make static                      # statically linked, for nodes that differ from the submit host
make mutation                    # ./mutation_tests/run.py
make state-properties            # python3 state_property_tests/run.py
make concurrency                 # python3 concurrency_tests/run.py
make load                        # python3 load_tests/run.py
make bottlenecks                 # python3 bottleneck_tests/run.py
```

Python is linted with `ruff` (config in `ruff.toml`, target py38, line-length 100) and type-checked
with `mypy --ignore-missing-imports --disable-error-code=import-untyped jobchain` (both run as
part of `run_tests.sh` when installed).
The C helper builds under `-Wall -Wextra -Werror -Wshadow -Wconversion -Wstrict-prototypes -std=c99
-pedantic`; the test suite always rebuilds it first (sanitized by default) so tests never run
against a stale binary.

`run_tests.sh` runs each `test_*.py` module in its own interpreter to prevent global
logger/environment/subprocess state from leaking between modules, while coverage's parallel mode
still produces one combined report.

## Architecture

The Python/C split mirrors the submit-host/compute-node split, since compute nodes are not assumed
to have a Python interpreter:

- **Python, submit host** (`jobchain/`): configuration, normalization, validation, script
  generation, submission, reporting, reconciliation, correction.
- **C, compute node** (`src/jobchain-node.c`, one file, compiled to `bin/jobchain-node`): exactly
  four operations — claim a row, record a status, emit a handoff value, submit the next row's
  pipeline. It never parses the delimited file or sees the schema; generation pre-renders each
  row's parameters into a shell fragment the job sources instead.
- **`bin/jobchain-node.sh`**: a POSIX-shell reimplementation of the same protocol, for sites
  without a C compiler. It passes the same conformance tests as the compiled helper but claiming
  becomes a process-spawn loop (`mkdir` isn't a shell builtin), so it's only recommended up to
  roughly 1,000 rows.

There is only one implementation of the claim protocol conceptually — Python shells out to the
helper rather than reimplementing claim/status logic itself.

### The claim protocol

A row is claimable when `run-<gen>` does not exist; claiming it is `mkdir(rows/000123/run-2)`.
`mkdir` either creates the directory or fails `EEXIST`, and NFS guarantees the server picks a
single winner, so exactly one caller wins however many nodes race. Consequences: retrying raises a
generation number (previous attempts survive); a dead claimer does not release its row (auto-release
could re-run the same parameters); editing a row is safe mid-run via a `hold` file.

### Module layout (`jobchain/`)

Dependencies run one way, downward — a lower module never imports a higher one:

```
core → schema → parse → config → pipeline → store → scheduler → operations → report → cli
```

`cli/` is the only package that parses arguments, prints for a human, or turns an exception into
an exit code.

| Module | Role |
|---|---|
| `core.py` | exit codes, exceptions, logging |
| `schema/` | validators, the Field/Schema model, YAML and Python loaders |
| `parse.py` | normalization, then the three-tier scan |
| `config.py` | run config: merge, capture, templates |
| `pipeline.py` | stages, class resolution, the `JobStage` interface |
| `store/` | row state, generations, claims |
| `scheduler.py` | submit, query, cancel, script generation |
| `operations/` | run, rerun, cancel, doctor, completion |
| `report.py` | status, show, metrics, export |
| `cli/` | argument parsing and exit-code mapping |

`schema/`, `store/`, `operations/`, and `cli/` are subpackages (each was a single file above this
size before a maintainability refactor split them along their own documented internal boundaries);
every name reachable at the flat-file path before that split is still reachable the same way today.

### The JobStage interface

```python
class JobStage:
    settings = {}                      # keys this stage accepts

    def __init__(self, name, config, run): ...
    def resources(self, row) -> dict: ...
    def output_dir(self, row, ctx) -> str: ...
    def script_name(self, row) -> str: ...
    def write_script(self, row, ctx) -> str: ...
```

One instance per stage, reused for every row, frozen after construction — stage classes are pure
functions of `(row, ctx)`, and assigning to `self` after construction raises.

Context objects (`RunContext`, `RowContext`) are created by jobchain in Python, on the submit host,
before any script is written; the scheduler never sees them and they don't exist at job time.
`RunContext.preamble()` writes `JC_RUN="${JC_RUN:-<this generation>}"` so a generated script honours
the run directory passed at submission but falls back to the generation it was written for — this
is what lets a script be resubmitted by hand, unmodified, months later.

### Chaining

The chaining stage claims the next row and submits its whole pipeline unconditionally on exit
status:

```sh
if [ "${JC_CHAIN:-0}" = "1" ]; then
    "$JC_NODE" submit --home "$JC_HOME" --next
fi
```

`JC_CHAIN` is exported by jobchain on submission and unset for a bare `qsub`, so a manual rerun
records its status and stops instead of continuing the chain. The residual risk: `afterany` fires
when the previous job *terminates*, not when the last stage never ran at all (wholesale
cancellation, rejected mid-pipeline submission, node death before start) — `doctor` is the only
thing that detects and repairs that, which is why it's load-bearing rather than optional.

### On-disk state

Everything lives under `.jobchain/<run name>/`; nothing there is hand-edited. Each row's attributes
are their own small file (`rows/<name>/meta.json`, `env`, `gen`, `manifest`, `handoff.seed`, and
per-generation `run-N/{claim,timeline,handoff,status.<stage>,jobid.<stage>,error.<stage>,
resources.<stage>.json}`), so a partial write can't corrupt an unrelated file and state stays
legible with `cat` on a compute node. Status files are replaced by rename; timeline/event entries
are single short `O_APPEND` writes. See README.md "On-disk state" for the full tree.

## Testing philosophy

The suite (1,321 tests in `tests/` at last count, ~99.7% branch coverage) is organized by *kind of
failure*, not just unit coverage. Categories, each with its own test directory/runner:

- **Unit + integration** (`tests/`) — `test_*.py` split into integration-style files (behavior,
  CLI, pipelines, persistence) and `test_*_unit.py` files (one per `jobchain/` module, mock-heavy).
- **Mutation testing** (`mutation_tests/`, `make mutation`) — deliberately broken semantic decisions
  must be killed by the existing suite.
- **State & property testing** (`state_property_tests/`, `make state-properties`) — generated state
  combinations exercise lifecycle invariants and status roll-ups.
- **Concurrency & race testing** (`concurrency_tests/`, `make concurrency`) — real processes contend
  for claims, locks, stop/resume state, generations.
- **Fault injection** (`fault_injection_tests/`) — filesystem/scheduler/helper failures and corrupt
  state, which must fail safely.
- **Load testing** (`load_tests/`, `make load`) — larger row sets, concurrent claim workloads.
- **Bottleneck & scaling testing** (`bottleneck_tests/`, `make bottlenecks`) — architecture-specific
  overload surfaces: `rows.idx` discovery, hot claim contention, large-run reporting, scheduler
  backpressure, increasing worker width. These look for nonlinear regressions, not fixed benchmark
  numbers.

Guarantees the suite enforces (don't regress these): one row has exactly one claim winner; the
compiled and shell node helpers implement one interchangeable protocol (including claiming against
each other); rows can be corrected and re-queued mid-run; normalization never changes a line's field
count; a helper killed mid-write leaves the old status or the new one, never a partial file; a
pipeline where every stage failed still advances the chain; generated scripts are valid POSIX shell
(`sh -n`); stage classes are frozen.

`examples/` doubles as user documentation and executable test fixtures (see `test_examples_e2e.py`
and `examples/README.md`) — small deterministic pipelines runnable against a real scheduler or the
repo's stub scheduler.
