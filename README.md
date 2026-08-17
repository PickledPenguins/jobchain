# jobchain

**Version 0.6**

Run scheduler job pipelines from a delimited parameter file. Each row is one
unit of work, and may be a single job or an ordered series of dependent jobs.

Rows are validated before anything is submitted. A fixed number of pipelines
advance concurrently, each submitting its successor as it finishes, so the
queue stays occupied without an external driver. Rows that fail can be found,
corrected, and re-run while the rest of the work continues.

---

## Contents

**Part I — Using jobchain**
[What it does](#what-it-does) ·
[Scope](#scope) ·
[Requirements](#requirements) ·
[Installation](#installation) ·
[Project structure](#project-structure) ·
[Quick start](#quick-start) ·
[Configuration](#configuration) ·
[Writing a schema](#writing-a-schema) ·
[Writing a pipeline](#writing-a-pipeline) ·
[Writing a stage class](#writing-a-stage-class) ·
[Command reference](#command-reference) ·
[Views and their fields](#views-and-their-fields) ·
[Correcting rows](#correcting-rows) ·
[Several runs at once](#several-runs-at-once) ·
[Completion](#completion) ·
[Exit status](#exit-status)

**Part II — Reference**
[Complete YAML reference](#complete-yaml-reference) ·
[Validator reference](#validator-reference) ·
[Pipeline order](#pipeline-order) ·
[Option behavior in detail](#option-behavior-in-detail) ·
[A worked example](#a-worked-example) ·
[Choosing between similar options](#choosing-between-similar-options) ·
[Task-to-options guide](#task-to-options-guide)

**Part III — Developer reference**
[Architecture](#architecture) ·
[The JobStage interface](#the-jobstage-interface) ·
[The context objects](#the-context-objects) ·
[The claim protocol](#the-claim-protocol) ·
[On-disk state](#on-disk-state) ·
[Chaining](#chaining) ·
[Logging](#logging) ·
[Module layout](#module-layout) ·
[Testing](#testing) ·
[Assumptions and limitations](#assumptions-and-limitations) ·
[Future work](#future-work) ·
[Changelog](#changelog)

---

# Part I — Using jobchain

## What it does

```
   submit host                                    compute nodes
   ───────────                                    ─────────────
   jobchain run config.yaml
      │
      ├─ normalize      repair syntax, never change field counts
      ├─ validate       every column of every row
      ├─ generate       one submit script per stage per row
      └─ submit ──────▶ [prep] ─▶ [solve] ─▶ [archive]
                                                  │
                          scheduler dependencies  │ claims the
                          chain the stages        │ next row
                                                  ▼
                                             [prep] ─▶ ...
```

Four principles shape everything below.

**A submit script is never tied to another job.** Dependencies live in the
arguments passed to `qsub` or `sbatch`, never inside the script. Any stage
script is a standalone artifact that can be resubmitted by hand, months later,
against the data a previous run left behind.

**Validation happens before any stage object exists.** A row that fails is
recorded and skipped; no stage class is ever constructed for it, so stage
authors never handle invalid input.

**The stage class owns its script completely** — its contents, its directives,
and where it is written. Complexity belongs in the class, not the
configuration.

**Every execution is isolated.** Two unrelated runs in one directory cannot
collide.

## Scope

**In scope.** Running a fixed number of concurrent scheduler pipelines from a
delimited parameter file, where rows are independent and may execute in any
order. Validating that file before submission. Finding, correcting, and
re-running failed rows while the run continues. Recovering when a chain dies.

**Out of scope**, and unlikely to change:

| Not handled | Use instead |
|---|---|
| Branching or conditional pipelines | A workflow engine; stages here are a sequence |
| Dependencies between rows | Same |
| Fan-out within a row | The scheduler's array jobs, or MPI inside a stage |
| Distributing work within one stage | MPI, or whatever the stage's command uses |
| Restarting a partially completed stage | The stage itself, which alone knows what partial means |
| Interpreting what stages exchange | Handoff values are untyped strings by design |

## Requirements

### Submit host

| Component | Requirement | Notes |
|---|---|---|
| Python | 3.8 or later | Standard library only, apart from PyYAML |
| PyYAML | 5.1 or later | Required; configuration is YAML |
| C compiler | Any C99 compiler | Used once, by `install.sh`. A shell helper is available instead |
| Scheduler client | `qsub` or `sbatch` | Not needed to validate or generate, only to submit |

### Compute nodes

| Component | Requirement |
|---|---|
| Shell | Any POSIX shell at `/bin/sh`. Bash is not required |
| Scheduler client | `qsub` or `sbatch`, callable from inside a job |
| Python | **Not required.** Nothing that runs inside a job uses it |

### Schedulers

| Scheduler | Versions | Notes |
|---|---|---|
| PBS Professional | 14 and later | `qsub -v`, `-W depend=`, `qstat -f -x`, `qdel` |
| OpenPBS | Should work | Shares the interfaces used here; untested |
| Slurm | 17.11 and later | `sbatch --export`, `--dependency=`, `squeue`, `sacct`, `scancel` |
| Torque | Not supported | Directive and query syntax differ enough to need its own backend |

### Storage

The run directory, the parameter file, and the helper must be on a filesystem
visible to both the submit host and the compute nodes, and that filesystem
must implement `mkdir` atomically. NFS, Lustre, and GPFS all do.
`jobchain doctor --check-fs` verifies it rather than assuming.

## Installation

```
./install.sh
```

Checks prerequisites, builds `bin/jobchain-node`, and verifies the filesystem.
No network access is used.

To install to shared storage, which is required because compute nodes execute
the helper:

```
./install.sh --prefix /shared/apps/jobchain
export PATH="/shared/apps/jobchain/bin:$PATH"
```

If compute nodes run a different image from the submit host, link statically
with `--static`. If the archive was extracted by something that dropped the
executable bit, run `sh install.sh`.

**Without a compiler**, use the shell helper, which implements the same
protocol:

```
export JOBCHAIN_NODE=/shared/apps/jobchain/bin/jobchain-node.sh
```

See [Architecture](#architecture) for what that costs.

**With network access and pip**, `pip install .` (or `pip install -e .` for
development) works too, using `pyproject.toml`, and installs a `jobchain`
console script alongside the Python package. It does not build
`bin/jobchain-node`; the C helper still needs `install.sh` or `make`, and
either needs to end up on `PATH` or be pointed to with `JOBCHAIN_NODE`.

## Project structure

```
jobchain-0.6/
├── README.md                   this document
├── DESIGN.md                   the architecture and the reasoning behind it
├── CHANGELOG.md                version history
├── Makefile                    builds the C helper: all, debug, static, clean
├── install.sh                  offline installer and environment check
├── run_tests.sh                test runner with coverage and static analysis
├── ruff.toml                   lint configuration
├── pyproject.toml              packaging metadata, for `pip install`
│
├── bin/
│   ├── jobchain                launcher; resolves paths and calls Python
│   ├── jobchain-node           compiled helper (built by install.sh)
│   └── jobchain-node.sh        shell helper, same protocol            363
│
├── jobchain/                   submit-host tool, Python
│   ├── __init__.py             public re-exports, including the stage API 102
│   ├── __main__.py             entry point for "python3 -m jobchain"      9
│   ├── core.py                 exit codes, exceptions, logging          251
│   ├── schema.py               validators, the Field/Schema model,
│   │                           and its YAML and Python loaders        1,210
│   ├── parse.py                normalization, then the three-tier scan   524
│   ├── config.py               run config: merge, capture, templates     380
│   ├── pipeline.py             stages, class resolution, JobStage        543
│   ├── store.py                row state, generations, claims            860
│   ├── scheduler.py            submit, query, cancel, script generation  519
│   ├── operations.py           run, rerun, cancel, doctor, completion  1,244
│   ├── report.py               status, show, metrics, export             643
│   └── cli.py                  argument parsing and exit-code mapping    998
│
├── src/
│   └── jobchain-node.c         compute-node helper, C99, one file      1,043
│
├── examples/
│   ├── pipeline/                a three-stage pipeline, ready to run
│   │   ├── solver.yaml          configuration, schema and pipeline inline
│   │   ├── stages.py            the stage classes
│   │   ├── runs.psv             parameters, with two deliberately bad rows
│   │   └── data/                input files the rows reference
│   ├── simple/                  one configuration idea at a time
│   └── moderate/                combinations: handoff, depends, checks,
│                                 Slurm, the shell node helper, rerun
│
└── tests/
    ├── helpers.py              fixtures, a dependency-honouring stub     432
    ├── test_validators.py      every validator, happy and bad paths      419
    ├── test_schema_scan.py     schema loading, normalization, scan       464
    ├── test_config.py          configuration merge, templates, capture   209
    ├── test_pipeline.py        stage resolution, settings, freezing      362
    ├── test_node.py            both helpers, concurrency, crashes        451
    ├── test_integration.py     whole runs, chaining, correction          477
    ├── test_cli.py             every command, end to end                 413
    ├── test_multirun.py        isolation between concurrent runs         183
    ├── test_report_scheduler.py  views, metrics, directives              445
    ├── test_errors.py          failure paths and exit codes              290
    ├── test_examples_e2e.py    the examples/moderate/ projects, run for real
    ├── test_security.py        malformed input, injection, hostile state
    │
    │                          -- mock-heavy unit coverage, one file per
    │                             jobchain/ module, complementing the
    │                             integration-style files above --
    ├── test_core_unit.py       core.py: exceptions, logging, __main__
    ├── test_config_unit.py     config.py: RunConfig, templates, capture
    ├── test_schema_unit.py     schema.py: loading, YAML and Python schemas
    ├── test_parse_unit.py      parse.py: normalization and scanning
    ├── test_pipeline_unit.py   pipeline.py: settings, stage construction
    ├── test_scheduler_unit.py  scheduler.py: submission, directives, context
    ├── test_store_unit.py      store.py: row/run state, claims, parsing
    ├── test_operations_unit.py operations.py: run/rerun/doctor branches
    ├── test_report_unit.py     report.py: rendering and metrics branches
    └── test_cli_unit.py        cli.py: argument handling branches
```

Run-time files live under `.jobchain/<run name>/`; see
[On-disk state](#on-disk-state). Nothing there is edited by hand.

## Quick start

A parameter file:

```
run_id|input_file|output_dir|mode|threads|mesh_size|tolerance
r001|data/alpha.h5|out/alpha|cpu|16|medium|1e-6
r002|data/beta.h5|out/beta|gpu|32|large|1e-8
```

A configuration describing the format, the pipeline, and how to run it:

```yaml
# solver.yaml
name: solver-production
params: runs.psv
width: 8

schema:
  format: {delimiter: pipe, header: true, id_field: run_id}
  fields:
    - {name: run_id,     type: regex, pattern: "[A-Za-z0-9_-]+"}
    - {name: input_file, type: path_exists, must_be_file: true}
    - {name: output_dir, type: str, unique: true}
    - {name: mode,       type: one_of, values: [cpu, gpu], case_sensitive: false}
    - {name: threads,    type: int, min: 1, max: 128}
    - {name: mesh_size,  type: one_of, values: [small, medium, large]}
    - {name: tolerance,  type: float, min: 0.0, max: 1.0}

pipeline:
  stage_module: stages.py
  stages:
    - {name: prep,    walltime: "00:30:00", ncpus: 2}
    - {name: solve,   depends: afterok}
    - {name: archive, depends: afterany}
```

Check it, run it, watch it:

```
jobchain run solver.yaml --check
jobchain run solver.yaml
jobchain status --watch
```

For a cautious first run set `width: 1`, watch one pipeline through, then run
the same command again to bring the rest up.

Relative paths in the parameter file resolve against **the parameter file's
own directory**, so a file validates identically wherever it is invoked.

## Configuration

One file configures a run. It may hold the schema and pipeline inline, or
point at separate files when they are shared.

```yaml
name: solver-production
description: Nightly production solve over the full parameter sweep.

params: runs.psv
width: 8
strict: false
workers: 8

schema: {...}           # inline, or:  schema: schema.yaml
pipeline: {...}         # inline, or:  pipeline: pipeline.yaml

paths:
  work_dir: "{row.output_dir}"
  log_dir: "{run.home}/logs"

logging:
  terminal: info
  file: debug

on_complete: "mail -s 'solver done' me@example.org < {run.home}/done.json"
```

**There are no `--schema` or `--pipeline` options.** If those are separate
files, their paths belong here, so that one file is the complete description
of a run.

### Templates

| Token | Expands to |
|---|---|
| `{row.<column>}` | A validated column value |
| `{row.name}` | The padded row name, `000123` |
| `{row.index}` | Position among data rows |
| `{row.generation}` | The row's current attempt number |
| `{run.name}`, `{run.home}` | The run's name and state directory |
| `{date}`, `{time}`, `{user}` | Expanded once, at load |

`{row.generation}` in `work_dir` namespaces output per attempt, so re-running
never overwrites a previous result.

### What overrides what

```
built-in defaults  ─▶  run configuration  ─▶  command line
```

### Configuration capture

| File | Contents |
|---|---|
| `config.original.yaml` | Exactly what was passed, byte for byte |
| `config.final.yaml` | The effective configuration after merging |

`config.final.yaml` is complete and runnable, with paths made absolute and a
comment on each non-default value recording its source:

```yaml
width: 16              # from the cli
scheduler: pbs         # from the config
```

`jobchain run config.final.yaml` reproduces a run exactly.

## Writing a schema

```yaml
format:
  delimiter: pipe        # or a literal character, or: comma tab colon
                         #    semicolon space whitespace
  header: true           # default false
  comment: "#"
  quoting: false         # honour quoted fields via CSV rules
  id_field: run_id       # identifies a row; implies unique: true

fields:
  - name: run_id
    type: regex
    pattern: "[A-Za-z0-9_-]+"

  - name: case_name
    type: str
    unique: true         # usable as --row case_name=somecase

  - name: ngpus
    optional: true       # empty is permitted
    default: 0
    type: int
    min: 0

row_checks:
  - {type: required_when, when_field: mode, equals: gpu, require_field: ngpus}
  - {type: compare, left: ngpus, op: "<=", right: threads}

file_checks:
  - {type: unique, fields: [output_dir]}
```

Columns match fields **by position**. A header that disagrees with the field
names produces a warning, because that usually means the wrong schema.

### Unique columns and row lookup

`unique: true` makes validation fail the file if values repeat, and makes the
column usable to name a row:

```
jobchain show  --row case_name=somecase
jobchain rerun --row run_id=r047 --set threads=64
jobchain show  --row 000123        # state directory name
jobchain show  --row 47            # row number
jobchain show  --row line:112      # source line number
```

`id_field` is optional and explicit — jobchain never picks a column
automatically. Without it, rows are identified by their padded name.

### Validation in a class

```yaml
schema:
  format: {delimiter: pipe, id_field: run_id}
  validator_class: validators.py
```

```python
from jobchain import Field, Int, OneOf, PathExists, Regex, SchemaBase


class SolverInput(SchemaBase):
    """Validation for the solver parameter file."""

    fields = [
        Field("run_id",     [Regex("[A-Za-z0-9_-]+")], unique=True),
        Field("input_file", [PathExists(must_be_file=True, readable=True)]),
        Field("threads",    [Int(min=1, max=128)]),
        Field("mode",       [OneOf(["cpu", "gpu"], case_sensitive=False)]),
    ]

    def check_row(self, row):
        """Cross-column rules awkward to express declaratively."""
        if row["mode"] == "gpu" and row["threads"] % 8:
            return "gpu mode requires threads to be a multiple of 8"
        return None
```

Both forms produce the same objects and may be mixed.

## Writing a pipeline

```yaml
stage_module: stages.py        # one module holds every stage class

defaults:                      # applied to every stage unless overridden
  queue: normal
  account: proj1

stages:
  - {name: prep, walltime: "00:30:00", ncpus: 2, mem: 8gb}
  - {name: solve_coarse, uses: Solve, depends: afterok, mesh: coarse}
  - {name: solve_fine,   uses: Solve, depends: afterok, mesh: fine}
  - {name: archive, depends: afterany, walltime: "02:00:00"}
```

**Stage order is submission order.** `depends` is the scheduler dependency
between a stage and the one before it.

| `depends` | Runs |
|---|---|
| `afterok` | Only if the previous stage succeeded. The default |
| `afterany` | Once the previous stage terminates, whatever its status |
| `afternotok` | Only if the previous stage failed. For diagnostics |

The scheduler evaluates these against the previous job's exit status:

| Previous job | `afterok` | `afterany` | `afternotok` |
|---|---|---|---|
| Exits 0 | runs | runs | cancelled |
| Exits non-zero | cancelled | runs | runs |
| Killed at walltime | cancelled | runs | runs |
| Cancelled while running | cancelled | runs | runs |
| Cancelled while queued, never ran | never satisfied | never satisfied | never satisfied |

The last row is why `jobchain cancel` removes every stage of a row explicitly
rather than trusting a cascade, and why `doctor` exists.

### Stage names and classes

A stage's `name` is a label; its class comes from `uses`, defaulting to the
name with each underscore-separated word capitalized.

| Stage name | `uses` | Class |
|---|---|---|
| `prep` | — | `Prep` |
| `mesh_refine` | — | `MeshRefine` |
| `solve_coarse` | `Solve` | `Solve` |

So two stages may share an implementation with different configuration, and
renaming a stage never breaks code. A stage with neither a resolvable class
nor a `command` is an error naming what the module does contain.

### Chaining

The **last stage** claims the next row, and depends `afterany` so the chain
survives an earlier failure. Both are defaults; a pipeline that says nothing
about chaining behaves correctly. Moving `chains_next` elsewhere requires that
stage to be `afterany` explicitly.

## Writing a stage class

```python
from jobchain import Choice, JobStage


class Solve(JobStage):
    """Main solver. Resources scale with the row's mesh size."""

    # Keys this stage accepts beyond the resource keys, validated against the
    # YAML at load time, so a typo is caught before anything is generated.
    settings = {"mesh": Choice(["coarse", "fine"], default="fine")}

    WALLTIME = {"coarse": "01:00:00", "fine": "16:00:00"}

    def resources(self, row):
        """Merged over the stage's YAML block: return only what varies."""
        return {"walltime": self.WALLTIME[self.config["mesh"]],
                "ncpus": row["threads"],
                "ngpus": 2 if row["mode"] == "gpu" else 0}

    def output_dir(self, row, ctx):
        """Where this stage's script is written for this row."""
        return f"{row['output_dir']}/scripts"

    def write_script(self, row, ctx):
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.effective_resources(row))}
{ctx.preamble()}

solver --mesh "$JC_OUT_mesh_file" \\
       --input "{row['input_file']}" \\
       --out "{ctx.work_dir}/result.h5"
rc=$?

{ctx.emit('result', ctx.work_dir + '/result.h5')}

{ctx.epilogue()}
exit $rc
""")
```

Three rules:

**One instance per stage, reused for every row.** Four stages create four
objects, however many rows there are.

**Instances are frozen.** Assigning to `self` outside `__init__` raises
immediately, which is what makes generating scripts across a thread pool safe
without asking stage authors to think about it. Lookup tables belong in class
attributes.

**The work must not exit the script directly.** The epilogue records the
status, so a stage that calls `exit` before reaching it leaves the row
recorded as running. Capture the status in `rc` and exit at the end.

A stage needing no class supplies a command:

```yaml
- {name: cleanup, depends: afterany, command: "rm -rf {row.output_dir}/tmp"}
```

## Command reference

| Command | Purpose |
|---|---|
| `run CONFIG` | Prepare and submit. State-aware: repeating it does what remains |
| `status` | How the run is going. Always a table |
| `show` | Everything about one row. Always sections |
| `rerun` | Run rows or stages again, optionally with changed values |
| `cancel` | Stop jobs, and with `--stop` take no new work |
| `doctor` | Reconcile against the scheduler; repair drift |
| `logs` | jobchain's own record of the run |
| `export` | Parameters and state as one delimited file |

Global options: `--run NAME`, `-v`/`-vv`, `--log-level`, `--file-log-level`,
`--json`, `--dry-run`.

### `run`

| State it finds | What it does |
|---|---|
| Nothing | Validate, generate, submit |
| Prepared, never submitted | Submit only |
| Running | Refuse, and report |
| Config or params changed since | Refuse, and say what changed |

| Option | Effect |
|---|---|
| `--check` | Validate only; write nothing |
| `--no-submit` | Validate and generate, but do not submit |
| `--submit-only` | Submit existing scripts without regenerating |
| `--regenerate` | Rebuild scripts before submitting |
| `--resume` | Clear the stop marker and relaunch chains |
| `-w`, `--width N` | Chains to run concurrently |
| `--workers N` | Threads used to generate scripts |
| `--run-name NAME` | Override the configured run name |
| `--strict` | Refuse to proceed if any row fails validation |
| `--force`, `--yes` | Discard an existing run; skip its confirmation |

### `status`

| Option | Effect |
|---|---|
| `--row SELECTOR` | One row, as a single table line |
| `--status STATUS` | Rows whose status starts with this, or whose category matches. Repeatable |
| `--stage NAME` | Rows currently at that stage |
| `--watch` | Repaint every five seconds |
| `--summary-only` | Counts and warnings, no table |
| `--metrics` | Add throughput, per-stage timing, a projection |
| `--all` | Every run, one line each |
| `--prune-after DAYS` | With `--all`, remove runs that finished longer ago than this. Lists them; needs `--yes` to remove |

### `show`

| Option | Effect |
|---|---|
| `--row SELECTOR` | The row to show |
| `--paths` | Only the artifact locations |
| `--stages` | Only the stage table |
| `--history` | Every generation |
| `--output` | The scheduler's own log for a stage |
| `--full` | Every section |
| `--invalid` | All rows that failed validation |

### `rerun`

Bare, it re-runs every stage for the selected rows at a new generation.

| Option | Effect |
|---|---|
| `--row SELECTOR` | Row to re-run; repeatable |
| `--status STATUS` | Every row matching; repeatable |
| `--set COL=VALUE` | Change a value first. Implies regeneration |
| `--stage NAME` | One stage only, at the current generation |
| `--stages A,B` | Those stages, in order |
| `--from NAME` | That stage and everything after it |
| `--chain` | Resume chaining from these rows |
| `--regenerate` | Rebuild scripts even without `--set` |
| `--fresh-handoff` | Start with an empty handoff |
| `--force`, `--yes` | Override guards; skip the typed confirmation |

### `cancel`, `doctor`, `logs`, `export`

| Command | Options |
|---|---|
| `cancel` | `--row`, `--status`, `--stage`, `--all`, `--stop` |
| `doctor` | `--repair`, `--all`, `--check-fs` |
| `logs` | `--follow`, `--level`, `--stage`, `--lines N` |
| `export` | `-o PATH`, `--status` |

## Views and their fields

### `run`

```
jobchain 0.6   run 'solver-production'

  shape       pipeline
  config      /scratch/proj/solver.yaml
  params      /scratch/proj/runs.psv
  scheduler   pbs
  width       8
  home        /scratch/proj/.jobchain/solver-production

Scanned 240 row(s) against schema 'solver-input': 238 valid, 2 invalid
generated 714 script(s)
row 000001 submitted: prep 4411, solve 4412, archive 4413
row 000002 submitted: prep 4414, solve 4415, archive 4416

!  2 row(s) were NOT submitted: they failed validation
```

The invalid-row block is last, so it is what remains on screen, and it repeats
at the top of every `status` until the count reaches zero.

### `status`

```
run 'solver-production'   Nightly production solve
home /scratch/proj/.jobchain/solver-production

[###################!!!..................] 96/238 (40.3%)
PENDING 110   QUEUED 24   RUNNING 8   DONE 93   failed 3   INVALID 2

!  2 row(s) failed validation and were never submitted
!  3 chain(s) live, configured width 8; chains may have been lost

ROW     ID    LINE  STATUS          STAGE    GEN  TRY  JOBID  ELAPSED  HOST
000094  r094  101   failed.solve.2  solve    1    1    4478   22m      node18
000097  r097  104   RUNNING         solve    1    1    4487   1.2h     node31
000098  r098  105   QUEUED          prep     1    1    4491   -        -
```

| Column | Meaning |
|---|---|
| `ROW` | State directory name, assigned in file order |
| `ID` | The `id_field` value, or the row name |
| `LINE` | Line number in the original parameter file |
| `STATUS` | Roll-up status for the row |
| `STAGE` | The stage reached; for a failure, the one that failed |
| `GEN` | Current generation; rises on each full rerun |
| `TRY` | Attempts claimed so far |
| `JOBID` | Job id of that stage |
| `ELAPSED` | Time in that stage |
| `HOST` | Execution host, from the row's timeline |

The bar shows `#` for succeeded, `!` for failed, `.` for outstanding.
**Warnings appear above the table**, never buried below it.

### `status --metrics`

```
Finished        96 of 238
Failure rate    3.1%
Per stage       prep       mean 8m       median 7m      failures 0
                solve      mean 1.9h     median 1.6h    failures 3
                archive    mean 11m      median 10m     failures 0
Wall elapsed    6.2h
Throughput      15.5 rows/hour
Projected left  9.2h   (assumes throughput holds)
Chains          8 of 8 live
```

Per-stage timing covers **successful** runs only, so failures do not distort
the picture of how long the work takes.

### `show`

```
row 000094   r094   line 101   generation 1   failed.solve.2

FAILURE
  stage       solve
  message     exit status 2
  job         4478.head on node18
  ran for     22m 11s

PARAMETERS
  input_file       '/data/psi.h5'
  mode             'gpu'
  threads          8

STAGES
  stage    status     job    depends   walltime  ncpus  mem    elapsed  host
  prep     DONE       4477   -         00:30:00  2      8gb    8m 33s   node18
  solve    FAILED     4478   afterok   16:00:00  8      32gb   22m 11s  node18
  archive  DONE       4480   afterany  02:00:00  1      -      4m 02s   node18

HANDOFF
  mesh_file        /scratch/proj/psi/mesh.h5

PATHS
  state       /scratch/proj/.jobchain/solver-production/rows/000094
  work        /scratch/proj/psi   412 files, 24.1 GB
  script      /scratch/proj/psi/scripts/01-prep.sh
  script      /scratch/proj/psi/scripts/02-solve.sh
  logs        /scratch/proj/.jobchain/solver-production/logs/000094
```

| Section | When it appears |
|---|---|
| `VALIDATION` | Only for a row that failed validation |
| `FAILURE` | Only for a failed row, and first |
| `PARAMETERS` | Always |
| `STAGES` | When a pipeline is configured |
| `HANDOFF` | When stages emitted values |
| `PATHS` | Always |
| `HISTORY` | With `--history` |

A healthy row prints three sections and about a dozen lines. The stage table
shows resources **as requested**, beside elapsed time, which is what
identifies a walltime kill without opening another view.

**Output is reported by directory with counts, never as a file listing**,
because a stage may produce thousands of files. Script and log paths are shown
in full, since those are individual files to open or resubmit.

### `doctor`

```
run 'solver-production'

chains       3 live, configured width 8        SHORTFALL 5
rows         238 total, 96 finished, 8 active, 134 pending

findings (4)
  [found] row 000101 stage solve is recorded RUNNING but job 4501.head is no
          longer known to the scheduler
  [found] row 000104 stage prep is QUEUED with no job id; the submitting
          process did not finish
  [found] 2 row(s) failed validation and were never submitted
  [found] 5 chain(s) short of the configured width 8

environment
  qsub             /opt/pbs/bin/qsub
  qstat            /opt/pbs/bin/qstat
```

`doctor` exists because a broken chain reports nothing: the run does not fail,
it quietly runs fewer chains and eventually none.

| Finding | What `--repair` does |
|---|---|
| Job vanished while active | Mark the stage failed |
| Claimed with no job id | Mark failed so the row can be re-queued |
| Chains below width | Launch enough to reach it |
| Parameter file changed | Nothing. Reported only |
| Script missing | Nothing. Reported only |

**Repair does not re-queue the rows it marks failed.** A row whose job
vanished may have written partial output, so that decision stays explicit.
It also cannot tell a slow job from a stuck one; walltime limits are the
scheduler's responsibility.

Safe to run at any time without `--repair`:

```
*/30 * * * * cd /scratch/proj && jobchain doctor --all --repair >> doctor.log 2>&1
```

### `logs`

jobchain's own record of the run: what it validated, submitted, and observed.

| Source | What it holds | How to read it |
|---|---|---|
| `<home>/jobchain.log` | jobchain's actions | `jobchain logs` |
| `<home>/logs/<row>/<stage>.log` | The stage's own output | `jobchain show --row X --output` |
| `<home>/rows/<row>/run-N/timeline` | One row's transitions | `jobchain show --row X --history` |

### `export`

Every original column, then state columns, so the result is still valid input
to the same schema.

| Appended column | Meaning |
|---|---|
| `status` | Roll-up status, including `failed.validation.<id>` |
| `stage` | The stage reached, or the one that failed |
| `generation`, `attempts` | Attempt counters |
| `elapsed_s` | Seconds in the reported stage |
| `work_dir` | The row's working directory |
| `error` | First line of the recorded error |

## Correcting rows

Nothing is mutated in place; corrections append.

| Goal | Command |
|---|---|
| Re-run a failed row | `jobchain rerun --row r094` |
| Fix a value and re-run | `jobchain rerun --row r094 --set threads=16` |
| Re-run from a stage onward | `jobchain rerun --row r094 --from solve` |
| Re-run one stage | `jobchain rerun --row r094 --stage solve` |
| Re-run every failure at a stage | `jobchain rerun --status failed.solve --from solve` |
| Bring an invalid row into the run | `jobchain rerun --row r047 --set threads=64` |
| Resume a stalled chain | `jobchain rerun --row r094 --chain` |
| Outside jobchain | `qsub /scratch/proj/psi/scripts/02-solve.sh` |

`--set` re-validates **before writing anything**, so a rejected correction
leaves the run exactly as it was. A correction takes the row out of
circulation with a hold file, rewrites it, and raises the generation last, so
a claimer sees either the old generation with the old parameters or the new
with the new.

An invalid row keeps state but has no scripts, which is what makes it
unclaimable until corrected — and what lets `--set` bring it into a run that
is already going.

### Protection against destroying results

| Rerunning | Confirmation |
|---|---|
| Failed, cancelled, or invalid rows | None. The normal case |
| A completed row whose output is gone | `--force` |
| A completed row whose output still exists | `--force` **and** a typed confirmation |

Whether output exists is checked, not assumed:

```
row 000042 (somecase) completed successfully. Output directories still exist:
  /scratch/proj/somecase   412 files, 24.1 GB

These are directories jobchain knows about. Stages may write elsewhere.
Type 'somecase' to confirm:
```

jobchain's own record is never at risk: a rerun creates `run-2/` beside
`run-1/`. Setting `work_dir: "{row.output_dir}/gen{row.generation}"`
namespaces output per attempt, after which nothing can be overwritten.

### Stopping a run

Cancelling active rows is not enough on its own: queued rows still run and
still chain. A stop marker closes that gap, checked before claiming.

| Goal | Command |
|---|---|
| Take no new work; let running jobs finish | `jobchain cancel --stop` |
| Stop everything now | `jobchain cancel --all` |
| Resume | `jobchain run config.yaml --resume` |

## Several runs at once

A run is identified by the `name` in its configuration, which becomes its
state directory, prefixes every job name, and selects it on the command line.
`name: "solver-{date}"` avoids collisions between days.

| Situation | Behavior |
|---|---|
| One run exists | Used automatically |
| `--run NAME` or `JOBCHAIN_RUN` given | That one |
| Several exist, none specified | **List them and stop.** Never guess |

```
$ jobchain status
3 runs exist; specify one with --run

NAME                ROWS  DONE  FAILED  ACTIVE  STARTED
solver-production    238    96       3       8  2026-08-12 22:14
mesh-study           400    17       0      16  2026-08-13 06:41
```

`show`, `rerun`, and `cancel` require a selection and never operate across
runs. `status --all` and `doctor --all` report on every run; the second is the
form worth running from cron.

Everything is isolated per run: state directory, log file, setup lock, row
claiming, and job names.

## Completion

When every row reaches a terminal state, `done.json` is written and the
`on_complete` hook runs.

```json
{
  "run": "solver-production",
  "completion": 2,
  "completed_at": "2026-08-13T04:12:08",
  "rows": {"total": 240, "done": 236, "failed": 2, "invalid": 2}
}
```

- Present only while nothing is outstanding; **removed the moment a row is
  re-queued**, so its presence always means "nothing outstanding right now".
- `completion` increments, distinguishing the first completion from one
  following corrections.
- `completions.log` keeps one line per completion.

The hook receives `JC_RUN_NAME`, `JC_COMPLETION`, `JC_ROWS_DONE`,
`JC_ROWS_FAILED`, and `JC_HOME`.

## Exit status

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Usage error |
| 2 | Internal error — a defect in this tool |
| 3 | The parameter file failed validation |
| 4 | The parameter file could not be parsed |
| 5 | The configuration, schema, or pipeline is invalid |
| 6 | The run directory is missing or inconsistent |
| 7 | Scheduler unavailable, or a submission was rejected |
| 8 | The helper is missing or failed |
| 9 | The operation conflicts with a running job |

**A traceback reaching the terminal always indicates a defect in this tool,
never bad input.**

Messages state what happened and stop. jobchain does not suggest follow-up
commands: hint text spread across dozens of messages drifts out of step with
the commands it names.

---

# Part II — Reference

## Complete YAML reference

### Run configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Run name and state directory. Supports `{date}`, `{time}`, `{user}` |
| `description` | string | — | Shown by `status` |
| `params` | path | required | The parameter file, relative to this file |
| `schema` | mapping or path | required | Inline schema, or a path |
| `pipeline` | mapping or path | — | Inline pipeline, or a path. Omitted means one job per row |
| `width` | int | 1 | Chains advancing concurrently |
| `max_attempts` | int | unlimited | Attempt cap per row |
| `max_in_flight` | int | unset | Ceiling on pipelines submitted but unfinished. Unset means no ceiling |
| `strict` | bool | `false` | Refuse to proceed if any row fails validation |
| `workers` | int | CPU count | Threads for script generation |
| `scheduler` | `pbs` \| `slurm` | `pbs` | Never detected |
| `on_complete` | string | — | Command run on completion |
| `logging.terminal` | level | `info` | Console verbosity |
| `logging.file` | level | `debug` | File verbosity |
| `logging.file_name` | string | `jobchain.log` | Log file name |
| `paths.work_dir` | template | `{run.home}/work/{row.name}` | Per-row working directory |
| `paths.log_dir` | template | `{run.home}/logs` | Scheduler output location |

### Schema

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | `schema` | Shown in reports |
| `version`, `description` | string | — | Free text |
| `format.delimiter` | char or alias | `,` | `comma`, `tab`, `pipe`, `colon`, `semicolon`, `space`, `whitespace`, or a literal |
| `format.header` | bool | `false` | Whether the first line names the columns |
| `format.comment` | string | `#` | Lines starting with this are ignored |
| `format.quoting` | bool | `false` | Honour quoted fields. Needs a single-character delimiter |
| `format.id_field` | column name | — | Identifies a row. Implies `unique: true` |
| `validator_class` | path | — | Module supplying validation instead of `fields` |
| `fields[]` | list | required unless `validator_class` | Column definitions, matched by position |
| `row_checks[]` | list | — | Cross-column checks |
| `file_checks[]` | list | — | Cross-row checks |

**Field entry**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Column name; becomes `JC_<name>` in the job |
| `description` | string | — | Shown in failure messages |
| `type` | string | — | Shorthand check, with arguments as sibling keys |
| `checks[]` | list of mappings | — | Explicit list when more than one check applies |
| `optional` | bool | `false` | Permits an empty value |
| `default` | any | `null` | Value yielded for an empty optional field |
| `unique` | bool | `false` | Enforce uniqueness; allow `--row name=value` |
| `python` | string | — | `file.py:NAME` reference to a `Validator` |

### Pipeline

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | `pipeline` | Pipeline name |
| `stage_module` | path | — | Module holding the stage classes |
| `defaults` | mapping | — | Resource defaults for every stage |
| `stages[]` | list | required | Ordered stages |

**Stage entry**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Stage label |
| `uses` | string | derived from `name` | Class implementing this stage |
| `depends` | enum | `afterok` | Dependency on the previous stage |
| `chains_next` | bool | last stage | Which stage claims the next row |
| `command` | string | — | For a stage with no class |
| `walltime` | string | — | `HH:MM:SS` |
| `nodes`, `ncpus`, `ngpus` | int | — | Resource counts |
| `mem` | string | — | For example `8gb` |
| `queue`, `account` | string | — | Queue and charge code |
| `extra_directives` | list | — | Lines passed through verbatim |
| `env` | mapping | — | Extra environment variables |
| *(class settings)* | — | — | Any key the class declares |

Unrecognized keys are a load-time error naming the stage.

## Validator reference

| Type | Arguments | Accepts |
|---|---|---|
| `int` | `min`, `max` | Optionally signed digits |
| `float` | `min`, `max`, `allow_nonfinite` | Decimal, optionally exponential |
| `str` | `min_length`, `max_length`, `charset` | Text |
| `bool` | — | `true/false`, `yes/no`, `on/off`, `1/0` |
| `one_of` | `values`, `case_sensitive` | Membership in a set |
| `exact` | `value` | One literal, compared as text |
| `regex` | `pattern`, `ignore_case` | Full-length match |
| `path_exists` | `must_be_file`, `must_be_dir`, `readable` | An existing path |
| `output_path` | `must_not_exist` | A path whose parent exists and is writable |
| `all_of`, `any_of` | `of` | Every, or at least one, child check |

**Row checks:** `required_when` (`when_field`, `equals`, `require_field`),
`compare` (`left`, `op`, `right`).
**File checks:** `unique` (`fields`), `row_count` (`min`, `max`).

### Two rejections worth knowing

**`int` rejects underscore separators.** `int("1_0")` returns 10 in Python, so
a typo would silently become a different number.

**`float` rejects `nan` and `inf`** unless `allow_nonfinite: true`. Both parse
successfully and neither is a meaningful job parameter.

### Normalization performed by checks

| Check | Transformation |
|---|---|
| all | Surrounding whitespace stripped |
| `int`, `float` | Converted, so `007` reaches the job as `7` |
| `bool` | Converted to `1` or `0` |
| `one_of` case-insensitive | Returns the member **as spelled in the schema** |
| `path_exists`, `output_path` | `~` and `$VAR` expanded; relative paths made absolute against the parameter file's directory |

## Pipeline order

```
  1  READ           file decoded as UTF-8; a byte-order mark removed
  2  SPLIT LINES    CRLF, CR, and LF normalized
  3  DROP           blank and comment lines, counted but discarded
  4  SPLIT FIELDS   on the delimiter, honouring quotes if enabled
  5  TRIM           whitespace stripped from each field
                    -> field count asserted unchanged from step 4
  6  HEADER         first surviving line consumed if declared
  ------------------------------------------------------------------ scan
  7  STRUCTURE      field count compared with the schema
                    -> a row failing here skips steps 8-11
  8  NORMALIZE      case folding, path expansion, numeric parsing
  9  FIELD CHECKS   in order; the first failure stops that field
                    -> a row failing here skips steps 10-11
 10  ROW CHECKS     relationships between columns, on converted values
 11  FILE CHECKS    uniqueness and counts, over rows that passed 7-10
  ------------------------------------------------------------------ gate
 12  GATE           with strict, any failure stops the run; otherwise
                    invalid rows are recorded and skipped
  ------------------------------------------------------------------ prepare
 13  ROW STATE      state written for every row, valid or not
 14  RENDER         one script per stage per valid row, in parallel
                    resources: pipeline defaults -> stage YAML -> class
 15  VERIFY         each script non-empty and parses as shell
  ------------------------------------------------------------------ run
 16  CLAIM          mkdir run-<gen>; exactly one caller wins
 17  SUBMIT         stages in order, threading dependency job ids
 18  EXECUTE        each stage sources env and handoff, runs, marks status
 19  CHAIN          the chaining stage claims and submits the next row
```

`run --check` stops after 12; `run --no-submit` after 15.

**Step 5 can never change the field count**, which stops a stray delimiter
from shifting every later column. **Step 8 runs before step 9**, so a check
always sees the canonical form of a value.

## Option behavior in detail

### Field checks, given an empty value

| Check | Given `""` | Given whitespace only | Value passed to the job |
|---|---|---|---|
| `int` | Fails | Fails, after trimming | Python `int` |
| `float` | Fails | Fails | Python `float` |
| `str` | **Passes** unless `min_length` set | Passes as `""` | The trimmed string |
| `bool` | Fails | Fails | `1` or `0` |
| `one_of` | Fails unless `""` is listed | Fails | The member as spelled in the schema |
| `exact` | Passes only if the literal is `""` | Fails | The raw string |
| `regex` | Passes if the pattern matches `""` | Fails | The raw string |
| `path_exists` | Fails | Fails | Absolute, expanded path |
| `output_path` | **Passes** — the parent exists | Passes | Absolute, expanded path |
| `optional: true` | **Passes**, yielding `default` | Passes | `default`, or `None` |

`str`, `exact` with an empty literal, `regex` with a zero-width pattern, and
`output_path` all accept an empty value without `optional`. If a column must
not be blank, say so: `str` with `min_length: 1`.

### Missing and extra columns

| Situation | Result |
|---|---|
| Fewer fields than the schema | Structural failure; field checks skipped |
| More fields than the schema | Structural failure; extras are not dropped |
| Header names disagree | Warning only. Columns match by **position** |
| No data rows | Scans clean unless `row_count` says otherwise |

### Resource resolution

| Situation | Result |
|---|---|
| Set in pipeline defaults only | Every stage and row uses it |
| Set in a stage block | That stage overrides the default |
| Returned by `resources()` | That row overrides both |
| Returned as an empty dict | YAML values used unchanged |
| Resolved value is `0` or `null` | No directive; the site default applies |
| Unrecognized key returned | Error listing the valid keys |

**Both YAML and class specifying the same key is expected**, not a conflict:
YAML holds the default, the class returns only what varies.

### Handoff values

| Situation | Result |
|---|---|
| A stage emits a key twice | Last value wins |
| A later stage reads an unset key | Empty string |
| Rerun at a new generation | Values carried forward as a seed |
| `--fresh-handoff` | The seed is dropped |
| Partial rerun with `--from` | Earlier stages' values remain available |

The seed lives beside the row, not inside the next generation's directory,
because creating that directory is how a row is claimed. Scripts source the
seed first and the generation's own handoff second, so a value emitted this
time overrides one carried forward.

## A worked example

One row, from the file to the queue.

**As written:** `r002|data/beta.h5 |/scratch/beta |GPU|32|large|1e-8`

| Step | Result |
|---|---|
| Split | 7 fields |
| Trim | Trailing spaces removed from fields 2 and 3; count still 7 |
| Structure | 7 fields, 7 declared: passes |
| `input_file` | Expanded to `/scratch/proj/data/beta.h5`, exists, readable |
| `mode` | `GPU` matched case-insensitively, canonicalized to `gpu` |
| `threads` | `32` → int 32, within 1–128 |
| `tolerance` | `1e-8` → float 1e-08, within 0–1 |
| Row checks | `mode == gpu` requires `ngpus`; `ngpus <= threads` holds |
| File checks | `run_id` and `output_dir` unique |

**Resources for `solve`**, merging three sources:

| Key | Value | Source |
|---|---|---|
| `walltime` | `16:00:00` | class, from `mesh_size: large` |
| `ncpus` | `32` | class, from `threads` |
| `mem` | `32gb` | stage YAML |
| `queue` | `normal` | pipeline defaults |

**Directives:**

```sh
#PBS -N solver-production-solve-000002
#PBS -l select=1:ncpus=32:mem=32gb:ngpus=2
#PBS -l walltime=16:00:00
#PBS -q normal
```

**Submitted:**

```
qsub                              01-prep.sh     -> 4415
qsub -W depend=afterok:4415       02-solve.sh    -> 4416
qsub -W depend=afterany:4416      03-archive.sh  -> 4417
```

## Choosing between similar options

**`run --check`, `--no-submit`, or `--dry-run`?** `--check` validates and
writes nothing. `--no-submit` also generates scripts, so they can be
inspected. `--dry-run` reports what any command would do without doing it.

**`rerun --stage` or `--from`?** `--stage` runs one stage at the current
generation, for re-running a step against existing data. `--from` runs that
stage and everything after it, which is the usual recovery.

**`rerun --set` or edit the parameter file?** `--set` changes one row inside
the run, immediately, while others keep running. Editing the file requires
`run --force`, discarding the run.

**`rerun` or `rerun --chain`?** Without `--chain` the rows run and stop. With
it, the row resumes chaining. Use it when a cancellation left the run short.

**`strict: true` or the default?** Strict suits a sweep only meaningful when
complete. The default submits valid rows and reports the rest prominently,
which suits independent units of work.

**`status` or `show`?** `status` is the run and always prints a table; `show`
is one row and always prints sections.

## Task-to-options guide

| Task | Command |
|---|---|
| Check a file before committing | `run config.yaml --check` |
| Prepare without submitting | `run config.yaml --no-submit` |
| Start a run | `run config.yaml` |
| Cautious first run | `width: 1`, run, watch, then run again |
| Watch progress | `status --watch` |
| See only failures | `status --status failed` |
| See rows that never ran | `show --invalid` |
| Diagnose one row | `show --row r094` |
| Find a row by name | `show --row case_name=somecase` |
| Locate output and scripts | `show --row r094 --paths` |
| Read a job's own output | `show --row r094 --output` |
| Estimate completion | `status --metrics` |
| Detect and repair lost chains | `doctor`, then `doctor --repair` |
| Re-run a failure | `rerun --row r094` |
| Fix a value and re-run | `rerun --row r094 --set threads=16` |
| Re-run every solve failure | `rerun --status failed.solve --from solve` |
| Rescue an invalid row | `rerun --row r047 --set threads=64` |
| Stop taking new work | `cancel --stop` |
| Stop everything | `cancel --all` |
| Resume | `run config.yaml --resume` |
| Collect results | `export -o results.psv` |
| Watch several runs | `status --all` |
| Clear out old finished runs | `status --all --prune-after 30 --yes` |
| Nightly health check | `doctor --all --repair` |
| Reproduce a run exactly | `run .jobchain/<name>/config.final.yaml` |

---

# Part III — Developer reference

## Architecture

The split between Python and C follows the split between submit host and
compute node, because compute nodes are not assumed to have an interpreter.

**Python, submit host.** Configuration, normalization, validation, script
generation, submission, reporting, reconciliation, correction.

**C, compute node.** Four operations: claim a row, record a status, emit a
handoff value, submit the next row's pipeline. It never parses the delimited
file and never sees the schema, because generation pre-renders each row's
parameters into a shell fragment the job sources.

There is **one implementation of the claim protocol**: Python shells out to
the helper rather than reimplementing it.

### Why the helper is compiled

A shell implementation ships alongside it and passes the same conformance
tests. Correctness is not the issue: `mkdir` is the same system call and
equally atomic, `>>` opens with `O_APPEND` so short appends do not interleave,
and a temporary renamed over its target is an atomic replacement.

Two things are lost. There is **no `fsync`**, so a node crash inside the write
window can leave an empty file. And **claiming becomes a process-spawn loop**,
because `mkdir` is not a shell builtin:

| Rows already claimed | Compiled | Shell |
|---|---|---|
| 100 | under 10 ms | roughly 0.2 s |
| 1,000 | under 50 ms | roughly 2 s |
| 10,000 | roughly 0.2 s | roughly 20 s |

Late in a large run every chain pays that walk, on node time. The shell helper
is therefore documented as suitable up to roughly a thousand rows, and for
sites that cannot compile.

## The JobStage interface

```python
class JobStage:
    settings = {}                      # keys this stage accepts

    def __init__(self, name, config, run): ...
    def resources(self, row) -> dict: ...
    def output_dir(self, row, ctx) -> str: ...
    def script_name(self, row) -> str: ...
    def write_script(self, row, ctx) -> str: ...
```

One instance per stage, reused for every row, frozen after construction.

## The context objects

Created by jobchain, in Python, on the submit host, before any script is
written. The scheduler never sees them and they do not exist at job time.

`RunContext`: `name`, `home`, `scheduler`, `node_binary`, `log_dir`,
`work_dir(row, row_name)`.

`RowContext`: `row_name`, `stage`, `row_dir`, `run_dir`, `env_file`,
`handoff`, `handoff_seed`, `work_dir`, `log_dir`, `script_path`.

Methods returning shell text: `directives(resources)`, `preamble()`,
`emit(key, value)`, `epilogue()`, `expand(text, row)`, `write(text)`.

`preamble()` writes `JC_RUN="${JC_RUN:-<this generation>}"`, so a script
honours the run directory passed at submission and falls back to the
generation it was written for. That is what lets a script be resubmitted at a
later generation without regenerating it, while a bare `qsub` months later
still records against the attempt it belongs to.

## The claim protocol

A row is claimable when `run-<gen>` does not exist; claiming it is
`mkdir(rows/000123/run-2)`. `mkdir` either creates the directory or fails with
`EEXIST`, and NFS guarantees the server decides which, so exactly one caller
wins however many nodes try at once.

Three consequences: **retrying is raising a number**, so previous attempts
survive; **a dead claimer does not release its row**, because automatic
release could run the same parameters twice; and **editing is safe during a
run**, because a `hold` file excludes a row while it is rewritten.

## On-disk state

```
.jobchain/solver-production/
├── config.original.yaml     exactly what was passed
├── config.final.yaml        effective configuration, runnable
├── jobchain.log             full-detail log
├── scan_report.json         validation result
├── rows.idx                 row names in file order
├── events.log               append-only
├── done.json                present only when nothing is outstanding
├── completions.log          one line per completion
├── stopped                  present only while the run is stopped
├── logs/000123/             scheduler output per row
└── rows/000123/
    ├── meta.json            identity, typed parameters, raw fields
    ├── env                  JC_<column> fragment
    ├── gen                  current generation
    ├── manifest             stage, depends, script path
    ├── handoff.seed         values carried forward from the last generation
    ├── hold                 present only during an edit
    └── run-1/
        ├── claim  timeline  handoff
        ├── status.<stage>   jobid.<stage>   error.<stage>
        └── resources.<stage>.json
```

Each attribute is its own small file, so a partial write cannot corrupt an
unrelated one and the state is legible with `cat` on a node. Status files are
replaced by rename; timeline and event entries are single short `O_APPEND`
writes.

## Chaining

The chaining stage claims the next row and submits its whole pipeline. The
call is not conditional on exit status, so a pipeline in which every stage
failed still advances the chain.

```sh
if [ "${JC_CHAIN:-0}" = "1" ]; then
    "$JC_NODE" submit --home "$JC_HOME" --next
fi
```

`JC_CHAIN` is exported by jobchain when it submits and unset for a bare
`qsub`, so a manual rerun records its status and stops.

**The residual risk.** `afterany` fires when the previous job *terminates*. It
does not fire when the last stage never runs at all — a wholesale
cancellation, a rejected mid-pipeline submission, or a node dying before it
started. `doctor` is the only thing that detects those, which is why it is
load-bearing rather than optional, and why `status` warns when chains are
below the configured width.

## Logging

Everything is logged twice: to the terminal at a level suited to watching, and
to `<home>/jobchain.log` at a level suited to diagnosis afterwards.

| Level | Content |
|---|---|
| `error` | Failures that stop work |
| `warning` | Invalid rows, attempt caps, lost chains |
| `info` | Phase transitions, submissions, status changes, chain events |
| `debug` | Script paths, resource merges, per-row claims |
| `trace` | Every file write, every subprocess invocation |

Output is reported by directory with counts, never as a file listing.
Directory scanning is depth-limited so a deep tree cannot delay a message.

Log files are per run, so concurrent executions never interleave.

## Module layout

Dependencies run one way, downward:

```
core ─▶ schema ─▶ parse ─▶ config ─▶ pipeline ─▶ store ─▶ scheduler
                                                            │
                                              operations ◀──┘
                                                    │
                                          report ◀──┘
                                             │
                                            cli
```

`cli.py` is the only module that parses arguments or prints for a person, and
the only place an exception becomes an exit code.

## Testing

The test suite is organized by the kind of failure it is intended to catch,
rather than relying on unit coverage alone.

### Load testing

Run `make load` to execute the dedicated bounded load/stability workloads.
These complement unit, mutation, state/property, concurrency, and fault-injection testing.

```
./run_tests.sh                 build, test, coverage, static analysis
./run_tests.sh --fast          skip coverage and the sanitizer build
./run_tests.sh test_node.py   one module

make mutation                  semantic mutation testing
make state-properties          generated state/property checks
make concurrency               process contention and race checks
make load                      bounded load/stability workloads
make bottlenecks               architecture-specific scaling tests
```

The helper is rebuilt first, so tests cannot pass against a stale binary, and
by default it is built with AddressSanitizer and UndefinedBehaviorSanitizer.

### Testing categories

- **Unit and integration tests** — behavior, validation, CLI, pipelines, and
  persistence.
- **Mutation testing** — deliberately broken semantic decisions must be killed
  by the existing tests; the current baseline is 9/9 mutations killed.
- **State & property testing** — generated state combinations exercise lifecycle
  invariants and status roll-ups.
- **Concurrency & race testing** — real processes contend for claims, locks,
  stop/resume state, and generations.
- **Fault injection** — filesystem replacement failures, scheduler failures and
  timeouts, helper failures, malformed helper output, and corrupt state.
- **Load testing** — larger row sets and concurrent claim workloads.
- **Bottleneck & scaling testing** — architecture-specific overload surfaces:
  sequential `rows.idx` discovery, hot claim contention, large-run reporting,
  scheduler backpressure, and increasing worker width.

**1,321 tests in the core suite alone, all passing** (`tests/`, run via
`unittest discover`), on top of the dedicated mutation, state/property,
concurrency, fault-injection, load, and bottleneck categories above. Combined
Python line coverage is 99.8%, branch coverage 99.7%; every `jobchain/`
module reads 99% or 100%, `__main__.py` excepted (a two-line interpreter
entry point that coverage cannot instrument). Run `./run_tests.sh` for the
current numbers; they change as the suite grows, so treat any number here as
a snapshot rather than a guarantee.

### What the suite guarantees

- **One row, one winner.** Simultaneous claimers take distinct rows; many
  claimers against one row produce exactly one winner.
- **Both helpers implement one protocol.** The compiled and shell versions run
  the same conformance checks, including claiming against each other.
- **Correction during a run.** Rows are corrected and re-queued while claimers work.
- **The field-count invariant.** Normalization never changes a line's field count.
- **Crash safety.** A helper killed mid-write leaves the old status or the new
  one, never a partial file.
- **The chain survives failures.** A pipeline in which a stage fails still
  advances the chain, and `afterany` successors still run.
- **Dependencies are threaded at submission**, and a rejection mid-pipeline
  cancels what was already submitted.
- **Handoff isolation** between generations.
- **Stage classes are frozen**; assigning to `self` raises.
- **Run isolation.** Two runs over one parameter file do not interact.
- **Fault injection.** Filesystem rename failures, scheduler submission timeouts,
  helper execution failures, malformed helper output, and corrupt configuration
  are deliberately injected and must fail safely.
- **Generated scripts are valid POSIX shell**, verified with `sh -n`.
- **Scaling guards.** Discovery, claim contention, reporting, scheduler pressure,
  and worker-width tests look for catastrophic or nonlinear regressions rather
  than asserting machine-specific benchmark numbers.
- **Guards fire**: starting a running run, re-running an active or completed row,
  reusing a name.

### Static analysis

C under `-Wall -Wextra -Werror -Wshadow -Wconversion -Wstrict-prototypes
-std=c99 -pedantic`, plus the suite under sanitizers. Python under `ruff` and
`mypy`, both clean when installed.

`valgrind` and `cppcheck` were not available where this was developed and have
not been run. The sanitizer build covers much of the same ground; a `valgrind`
pass on the target cluster is worth doing before heavy use.

## Assumptions and limitations

**Assumptions**

- Compute nodes can call `qsub` or `sbatch`; the chain submits its successor
  from inside a running job.
- The run directory and helper are on shared storage where `mkdir` is atomic.
- Rows are independent and may execute in any order.
- Stage classes are pure functions of `(row, ctx)`, which the frozen-instance
  rule enforces.

**Limitations**

- **Stages cannot compute their own resources from earlier output.** Resources
  are fixed at submit time; a stage may branch internally on handoff values
  but cannot change its own reservation.
- **Path checks run on the submit host**, so a pass is strong evidence rather
  than a guarantee if compute nodes mount storage differently.
- **Scripts carry one scheduler's directives**, fixed at generation.
- **jobchain cannot protect a stage's output files.** Use `{row.generation}`
  in the work directory template.
- **The work must not exit a stage script before the epilogue**, or the status
  is never recorded.
- **Claiming scans rows in order**, so cost grows with rows examined. Past
  roughly 10,000 rows a cached index would be worth adding.
- **`doctor` is load-bearing** for chain continuity in cases `afterany` cannot
  cover, and only helps if it is run.
- **Timestamps are local wall clock** on the execution host.

## Future work

- `--batch-size K`: drain K rows per job, to amortize queue latency when
  per-row work is short.
- Queue-limit introspection before submitting.
- An advisory claim cursor, to flatten the index scan for very large runs and
  make the shell helper viable at any size.
- A submit-host poller for sites that forbid submission from compute nodes.
- Per-stage retry policy.
- An explicit `class:` override accepting a module path.
- Structured output records, if handoff values ever need to be machine-read
  rather than sourced.

## Changelog

See `CHANGELOG.md` for the full history. Version 0.5 introduced multi-stage
pipelines, run isolation, the single-file configuration, and reduced the
command surface to eight commands. Builds `0.5-v1b` through `0.5-v4b` are
bugfix builds on top of 0.5; see `BUGFIXES.md` for what changed. The `0.5v3c`
through `0.5v5c` builds added the state-property, concurrency, bottleneck,
and fault-injection test categories described under "Testing philosophy" in
`CLAUDE.md`. 0.6 unified the version identifiers those builds had scattered
across the Python package, the C helper, and the shell helper.
