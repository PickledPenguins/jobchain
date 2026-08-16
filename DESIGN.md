# jobchain — architecture and design

**Version 0.5 (proposed)**
**Status:** design complete, not yet implemented
**Supersedes:** the single-job-per-row model of jobchain 0.4

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
[Running several runs at once](#running-several-runs-at-once) ·
[Completion and notification](#completion-and-notification) ·
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
[Objects and responsibilities](#objects-and-responsibilities) ·
[The JobStage interface](#the-jobstage-interface) ·
[The context objects](#the-context-objects) ·
[The claim protocol](#the-claim-protocol) ·
[Why the node helper is compiled](#why-the-node-helper-is-compiled) ·
[On-disk state](#on-disk-state) ·
[Dependency submission](#dependency-submission) ·
[Chaining](#chaining) ·
[Logging](#logging) ·
[Parallel script generation](#parallel-script-generation) ·
[Module layout](#module-layout) ·
[Testing plan](#testing-plan) ·
[Assumptions and limitations](#assumptions-and-limitations) ·
[Future work](#future-work) ·
[Design decisions](#design-decisions) ·
[Changelog](#changelog)

---

# Part I — Using jobchain

## What it does

jobchain runs a series of scheduler jobs from a delimited parameter file. Each
row of the file is one unit of work. Each unit may be a single job or an
ordered pipeline of dependent jobs.

Rows are validated before anything is submitted. A fixed number of pipelines
advance concurrently, each submitting its successor as it finishes, so the
queue stays occupied without an external driver. Rows that fail can be found,
corrected, and re-run while the rest of the work continues.

```
   submit host                                    compute nodes
   ───────────                                    ─────────────
   jobchain run config.yaml
      │
      ├─ normalize      repair syntax, never change field counts
      ├─ validate       every column of every row
      ├─ generate       one submit script per stage per row
      └─ submit ──────▶ [prep] ─▶ [solve] ─▶ [reduce] ─▶ [archive]
                                                             │
                          scheduler dependencies             │ claims the
                          chain the stages                   │ next row
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

**The stage class owns its script completely** — its contents, its scheduler
directives, and the directory it is written to. Complexity belongs in the
class, not the configuration.

**Every execution is isolated.** Two unrelated runs in the same directory
cannot collide.

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
| Data staging | The stage script, or a site tool |
| Interpreting what stages exchange | Handoff values are untyped strings by design |

**Deferred**, listed in [Future work](#future-work): batching several rows per
job, queue-limit introspection, and a submit-host poller for sites that forbid
submission from compute nodes.

## Requirements

### Submit host

| Component | Requirement | Notes |
|---|---|---|
| Python | 3.8 or later | Standard library only, apart from PyYAML |
| PyYAML | 5.1 or later | Required; configuration is YAML |
| C compiler | Any C99 compiler | Used once, by `install.sh` |
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
| Slurm | 17.11 and later | `sbatch --export`, `--dependency=`, `squeue`, `sacct`, `scancel`. Works without accounting |
| Torque | Not supported | Directive and query syntax differ enough to need its own backend |

### Storage

The run directory, the parameter file, and the compiled helper must all be on
a filesystem visible to both the submit host and the compute nodes, and that
filesystem must implement `mkdir` atomically. NFS, Lustre, and GPFS all do.
`jobchain doctor --check-fs` verifies it directly rather than assuming.

## Installation

```
./install.sh
```

Checks prerequisites, builds `bin/jobchain-node`, and verifies that the
filesystem supports the claim protocol. No network access is used.

To install to shared storage, which is required because compute nodes execute
the helper:

```
./install.sh --prefix /shared/apps/jobchain
export PATH="/shared/apps/jobchain/bin:$PATH"
```

If compute nodes run a different image from the submit host, link statically:

```
./install.sh --prefix /shared/apps/jobchain --static
```

If the archive was extracted by something that dropped the executable bit, run
`sh install.sh`; it restores permissions on the way through.

## Project structure

```
jobchain-0.5/
├── README.md                   user and developer documentation
├── DESIGN.md                   this document
├── CHANGELOG.md                version history
├── Makefile                    builds the C helper: all, debug, static, clean
├── install.sh                  offline installer and environment check
├── run_tests.sh                test runner with coverage and static analysis
├── ruff.toml                   lint configuration
│
├── bin/
│   ├── jobchain                launcher; resolves paths and calls Python
│   └── jobchain-node           compiled helper (built by install.sh)
│
├── jobchain/                   submit-host tool, Python
│   ├── __init__.py             public re-exports, including the stage API
│   ├── __main__.py             entry point for "python3 -m jobchain"
│   ├── core.py                 exit codes, exceptions, logging
│   ├── schema.py               validators, Field/Schema model, loaders
│   ├── parse.py                normalization, then the three-tier scan
│   ├── config.py               run config: merge, capture, templates
│   ├── pipeline.py             stage definitions, class resolution, JobStage
│   ├── store.py                row state, generations, claims, run discovery
│   ├── scheduler.py            submit, query, cancel, directives, rendering
│   ├── operations.py           run, rerun, cancel, doctor
│   ├── report.py               status, show, metrics, export
│   └── cli.py                  argument parsing and exit-code mapping
│
├── src/
│   └── jobchain-node.c         compute-node helper, C99, one file
│
├── templates/
│   └── default_stage.sh.in     script template for stages without a class
│
├── examples/
│   ├── simple/                 one job per row, no pipeline
│   │   ├── solver.yaml
│   │   └── runs.psv
│   └── pipeline/               four-stage pipeline
│       ├── solver.yaml
│       ├── schema.yaml
│       ├── pipeline.yaml
│       ├── stages.py
│       └── runs.psv
│
└── tests/
    ├── helpers.py              fixtures, scheduler stubs, CLI runner
    ├── test_validators.py      every validator, happy and bad paths
    ├── test_schema_scan.py     schema loading, normalization, scan
    ├── test_config.py          config merge, capture, templates
    ├── test_pipeline.py        stage resolution, dependency rules
    ├── test_node.py            the C helper, concurrency, crashes
    ├── test_integration.py     store, chains, correction under load
    ├── test_cli.py             every command, end to end
    ├── test_multirun.py        isolation between concurrent runs
    └── test_errors.py          failure paths, exit codes, doc drift
```

Files created at run time live under `.jobchain/<run name>/` and are described
in [On-disk state](#on-disk-state). Nothing there is edited by hand.

## Quick start

A parameter file, one row per unit of work:

```
run_id|input_file|output_dir|mode|threads|mesh_size|tolerance
r001|data/alpha.h5|/scratch/proj/alpha|cpu|16|medium|1e-6
r002|data/beta.h5|/scratch/proj/beta|gpu|32|large|1e-8
r003|data/gamma.h5|/scratch/proj/gamma|cpu|8|small|1e-6
```

A configuration file describing the format, the pipeline, and how to run it:

```yaml
# solver.yaml
name: solver-production
params: runs.psv
width: 8

schema:
  format: {delimiter: pipe, id_field: run_id}
  fields:
    - {name: run_id,     type: regex, pattern: "[A-Za-z0-9_-]+"}
    - {name: input_file, type: path_exists, must_be_file: true}
    - {name: output_dir, type: output_path}
    - {name: mode,       type: one_of, values: [cpu, gpu], case_sensitive: false}
    - {name: threads,    type: int, min: 1, max: 128}
    - {name: mesh_size,  type: one_of, values: [small, medium, large]}
    - {name: tolerance,  type: float, min: 0.0, max: 1.0}

pipeline:
  stage_module: stages.py
  stages:
    - {name: prep,    walltime: "00:30:00", ncpus: 2}
    - {name: solve,   depends: afterok}
    - {name: reduce,  depends: afterok, walltime: "01:00:00", ncpus: 8}
    - {name: archive, depends: afterany, walltime: "02:00:00", ncpus: 1}
```

Check it without writing anything:

```
jobchain run solver.yaml --check
```

Run it:

```
jobchain run solver.yaml
jobchain status --watch
```

For a cautious first run, set `width: 1`, watch one pipeline through, then
`jobchain run solver.yaml` again to bring the rest up.

Relative paths in the parameter file resolve against **the parameter file's
own directory**, not the working directory, so a file validates identically
wherever it is invoked.

## Configuration

One file configures a run. It may contain the schema and pipeline inline, or
point at separate files when they are shared between runs.

```yaml
# solver.yaml — everything in one place
name: solver-production
description: Nightly production solve over the full parameter sweep.

params: runs.psv
width: 8
strict: false
workers: 8

schema:  {...}          # inline, or:  schema: schema.yaml
pipeline: {...}         # inline, or:  pipeline: pipeline.yaml

paths:
  work_dir: "{row.output_dir}"
  log_dir: "{run.home}/logs"

logging:
  terminal: info
  file: debug

on_complete: "mail -s 'solver done' me@example.org < {run.home}/done.json"
```

**There are no `--schema` or `--pipeline` command-line options.** If those are
separate files, their paths belong in this config. That keeps one file as the
complete description of a run, and makes `config.final.yaml` a faithful record.

### Templates

Any path value may use templates:

| Token | Expands to |
|---|---|
| `{row.<column>}` | A validated column value for that row |
| `{row.name}` | The padded row name, `000123` |
| `{row.index}` | The row's position among data rows |
| `{row.generation}` | The row's current attempt number |
| `{run.name}` | The run name |
| `{run.home}` | The run's state directory |
| `{date}`, `{time}`, `{user}` | Expanded once, at load |

`{row.generation}` in `work_dir` is worth knowing about: it namespaces output
per attempt, so re-running never overwrites a previous result.

### What overrides what

```
built-in defaults  ─▶  run config file  ─▶  command line
   (lowest)                                   (highest)
```

### Configuration capture

Two files are written into the run directory at setup:

| File | Contents |
|---|---|
| `config.original.yaml` | Exactly what was passed, byte for byte |
| `config.final.yaml` | The effective configuration after merging |

`config.final.yaml` is complete and runnable: schema and pipeline inlined,
paths absolute, every default explicit, and a comment on each non-default
value recording where it came from.

```yaml
width: 16              # cli: --width 16 (config file had 8)
workers: 8             # default: cpu count
scheduler: pbs         # detected
strict: false          # default
```

`jobchain run config.final.yaml` reproduces a run exactly, which is the point.

## Writing a schema

The schema describes how to split the parameter file and what each column must
contain. It may be inline in the run config or a separate file.

```yaml
name: solver-input
version: "2"

format:
  delimiter: pipe        # or a literal character, or: comma tab colon
                         #    semicolon space whitespace
  header: true           # first non-comment line names the columns (default false)
  comment: "#"           # lines starting with this are ignored
  quoting: false         # honour quoted fields via CSV rules
  id_field: run_id       # identifies a row; implies unique: true

fields:
  - name: run_id
    description: unique identifier for this parameter set
    type: regex
    pattern: "[A-Za-z0-9_-]+"

  - name: case_name
    description: human-readable case label
    type: str
    unique: true         # usable as --row case_name=somecase

  - name: input_file
    type: path_exists
    must_be_file: true
    readable: true

  - name: ngpus
    optional: true       # empty is permitted
    default: 0           # and yields this value
    type: int
    min: 0
    max: 8

  - name: threads
    checks:              # explicit form, when more than one check applies
      - {type: int, min: 1, max: 128}

row_checks:              # relationships within one row
  - type: required_when
    when_field: mode
    equals: gpu
    require_field: ngpus
  - type: compare
    left: ngpus
    op: "<="
    right: threads

file_checks:             # constraints across the whole file
  - type: unique
    fields: [output_dir]
```

Columns match fields **by position**, in the order listed. A header that
disagrees with the field names produces a warning, because that usually means
the wrong schema was selected.

### Unique columns and row lookup

`unique: true` on a field does two things: validation fails the file if the
values are not distinct, and the column becomes usable to name a row.

```
jobchain show  --row case_name=somecase
jobchain rerun --row run_id=r047 --set threads=64
jobchain show  --row 000123               # state directory name
jobchain show  --row 47                   # row number
jobchain show  --row line:112             # source line number
```

`id_field` is optional and explicit. jobchain never picks a column
automatically and never invents one.

| `id_field` set | What identifies a row |
|---|---|
| Yes | That column's value, shown in the `ID` column of every table |
| No | The padded row name, `000123`, which always exists |

`id_field` implies `unique: true`, so the identifying column never needs both.
Any column marked `unique` can be used for lookup, whether or not it is the
`id_field`. Naming a column that is not unique is an error that lists the
columns that are.

### Validation in a class

As an alternative to declaring fields in YAML, a schema may name a module:

```yaml
schema:
  format: {delimiter: pipe, id_field: run_id}
  validator_class: validators.py
```

```python
from jobchain import Field, Int, OneOf, PathExists, Regex, SchemaBase


class SolverInput(SchemaBase):
    """Validation for the solver parameter file.

    Field order defines column order, exactly as the YAML list does.
    """

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

    def check_file(self, rows):
        """Cross-row rules. Returns (line number, reason) pairs."""
        return []
```

Both forms produce the same objects and may be mixed: declare `fields` in
YAML and add row checks from a class, or attach one custom validator to a
single field with `python: file.py:NAME`.

## Writing a pipeline

```yaml
name: solver-pipeline
version: "1"
description: Four-stage solve, from mesh preparation through archival.

stage_module: stages.py        # one module holds every stage class

defaults:                      # applied to every stage unless overridden
  queue: normal
  account: proj1
  nodes: 1

stages:
  - name: prep                 # -> class Prep
    walltime: "00:30:00"
    ncpus: 2
    mem: 8gb

  - name: solve_coarse         # -> class Solve, configured for coarse
    uses: Solve
    depends: afterok
    mesh: coarse

  - name: solve_fine           # -> class Solve, configured for fine
    uses: Solve
    depends: afterok
    mesh: fine

  - name: archive              # -> class Archive
    depends: afterany          # runs even if an earlier stage failed
    walltime: "02:00:00"
```

**Stage order is submission order.** `depends` is the scheduler dependency
between a stage and the one before it.

| `depends` | Meaning |
|---|---|
| `afterok` | Runs only if the previous stage succeeded. The default |
| `afterany` | Runs once the previous stage terminates, whatever its status |
| `afternotok` | Runs only if the previous stage failed. For diagnostics |

The scheduler evaluates this against the previous job's exit status, which is
the stage script's exit status. jobchain's own status recording is separate
and happens regardless of what the scheduler then does.

| Previous job | `afterok` | `afterany` | `afternotok` |
|---|---|---|---|
| Exits 0 | runs | runs | cancelled |
| Exits non-zero | cancelled | runs | runs |
| Killed at walltime | cancelled | runs | runs |
| Cancelled while running | cancelled | runs | runs |
| Cancelled while queued, never ran | never satisfied | never satisfied | never satisfied |
| Held indefinitely | waits | waits | waits |

The fifth row is the one that matters. A job removed from the queue before it
ran never terminates, so no dependency type fires. PBS and Slurm differ in
what they do with the stranded dependents, and Slurm's behaviour further
depends on `kill_invalid_depend`, so jobchain relies on neither: `cancel`
explicitly removes every stage of a row rather than trusting the scheduler to
cascade, and `doctor` detects any that survive.

### Stage names and classes

A stage's `name` is a label. Its class comes from `uses`, defaulting to the
name with each underscore-separated word capitalized.

| Stage name | `uses` | Class used |
|---|---|---|
| `prep` | — | `Prep` |
| `mesh_refine` | — | `MeshRefine` |
| `solve_coarse` | `Solve` | `Solve` |

This means two stages may share an implementation with different
configuration, a stage may be named for its role without inventing a matching
class, and renaming a stage in the config never breaks code.

If `stage_module` is set and a stage has neither a resolvable class nor a
`command`, that is an error naming both what was sought and what the module
contains — never a silent fallback.

```
error: pipeline stage 'reduce' has no class 'Reduce' in stages.py and no
       'command'. stages.py defines: Prep, Solve, Archive
```

### Chaining

The **last stage** claims the next row, and it must be `afterany` so the chain
survives an earlier failure. Both are defaults, so a pipeline that says nothing
about chaining behaves correctly.

Moving `chains_next` to another stage is allowed, and that stage must also be
`afterany`:

```
error: stage 'reduce' sets chains_next but depends 'afterok'; a chaining
       stage must depend 'afterany', or the chain stops whenever an earlier
       stage fails
```

See [Chaining](#chaining) for the mechanism and its one residual risk.

## Writing a stage class

A stage class generates a submit script. It runs on the submit host at
generation time and never on a compute node.

```python
from jobchain import Bool, Choice, JobStage


class Solve(JobStage):
    """Main solver stage. Resources scale with the row's mesh size."""

    # Configuration keys this stage accepts, beyond the standard resource
    # keys. Validated against the YAML at load time, so a typo is caught
    # before anything is generated.
    settings = {
        "mesh": Choice(["coarse", "fine"], default="fine"),
        "restart": Bool(default=False),
    }

    WALLTIME = {"coarse": "01:00:00", "fine": "16:00:00"}

    def resources(self, row):
        """Scheduler parameters for this row.

        Merged over the stage's YAML block, so only what varies per row
        needs returning.
        """
        return {
            "walltime": self.WALLTIME[self.config["mesh"]],
            "ncpus": row["threads"],
            "ngpus": 2 if row["mode"] == "gpu" else 0,
        }

    def output_dir(self, row):
        """Where this stage's script is written for this row."""
        return f"{row['output_dir']}/scripts"

    def write_script(self, row, ctx):
        """Write this row's script for this stage; return its path."""
        return ctx.write(f"""#!/bin/sh
{ctx.directives(self.resources(row))}
{ctx.preamble()}

. "{ctx.handoff}"

solver --mesh "$JC_OUT_mesh_file" \\
       --input "{row['input_file']}" \\
       --tol {row['tolerance']} \\
       --out "{ctx.work_dir}/result.h5"
rc=$?

{ctx.emit('result', f"{ctx.work_dir}/result.h5")}

{ctx.epilogue()}
exit $rc
""")
```

Three rules govern stage classes:

**One instance per stage, reused for every row.** A pipeline of four stages
creates four objects, however many rows there are.

**Instances are frozen after construction.** Assigning to `self` outside
`__init__` raises immediately. Stage classes are pure functions of
`(row, ctx)`, which is what makes parallel generation safe without any
thread-safety burden on the author. Lookup tables belong in class attributes,
as `WALLTIME` above.

**A stage may ignore every helper.** `ctx.directives`, `ctx.preamble`, and
`ctx.epilogue` are conveniences. A class wanting a completely different
template writes whatever it likes and returns the path.

A stage needing no class at all just supplies a command:

```yaml
- name: cleanup
  depends: afterany
  command: "rm -rf {row.output_dir}/tmp"
```

## Command reference

Six commands do the work. Two more produce output.

| Command | Purpose |
|---|---|
| `run` | Prepare and submit. The normal entry point |
| `status` | How the run is going |
| `show` | Everything about one row |
| `rerun` | Run rows or stages again, optionally with changed values |
| `cancel` | Stop jobs |
| `doctor` | Reconcile against the scheduler and repair drift |
| `logs` | The run log |
| `export` | Parameters and state as one delimited file |

Global options, accepted everywhere:

| Option | Effect |
|---|---|
| `--run NAME` | Which run to act on. Only needed when several exist |
| `-v`, `-vv` | Console detail: progress, then full trace |
| `--log-level`, `--file-log-level` | Set either sink explicitly |
| `--json` | Machine-readable output |
| `--dry-run` | Report what would happen; change nothing |

### `run`

```
jobchain run CONFIG [options]
```

`run` is state-aware. It looks at what already exists and does what remains.

| State it finds | What it does |
|---|---|
| Nothing | Validate, generate, submit |
| Prepared, never submitted | Submit only |
| Running | Refuse, and report status |
| Complete | Refuse, and report the summary |
| Prepared, but config or params changed since | Refuse, and say what changed |

| Option | Effect |
|---|---|
| `--check` | Validate only. Write nothing, submit nothing |
| `--no-submit` | Validate and generate, but do not submit |
| `--submit-only` | Submit existing scripts without regenerating |
| `--regenerate` | Rebuild scripts before submitting |
| `--width N` | Override the configured width |
| `--workers N` | Threads for script generation. Defaults to the CPU count |
| `--run-name NAME` | Override the run name from the config |
| `--strict` | Refuse to proceed if any row fails validation |
| `--resume` | Clear the stop marker and relaunch chains to width |
| `--force` | Discard an existing run of the same name |
| `--yes` | Skip the confirmation `--force` would otherwise require |

### `status`

```
jobchain status [options]
```

| Option | Effect |
|---|---|
| `--row SELECTOR` | One row, as a single table line |
| `--watch` | Repaint every five seconds until the run finishes |
| `--status STATUS` | Only rows with that status. Repeatable |
| `--stage NAME` | Only rows currently at that stage |
| `--summary-only` | Counts and warnings, no table |
| `--metrics` | Add throughput, per-stage timing, and a projection |
| `--all` | Every run, one line each |
| `--prune-after DAYS` | With `--all`, remove state for runs finished longer ago than this. Requires `--yes` |

`status` and `show` divide by output shape rather than by subject:
**`status` always prints a table, `show` always prints sections.** Use
`status --row` to see where one row sits among the others, and `show --row`
to read everything known about it.

### `show`

```
jobchain show --row SELECTOR [options]
```

Everything known about one row, in sections sized to the situation: a healthy
row prints a short summary, a failed row prints the failure detail first.

| Option | Effect |
|---|---|
| `--paths` | Only the artifact locations |
| `--stages` | Only the per-stage table |
| `--history` | Every generation, not just the current one |
| `--output` | The scheduler's own log for the failing stage |
| `--full` | Every section regardless of state |
| `--invalid` | All rows that failed validation, instead of one row |

### `rerun`

```
jobchain rerun --row SELECTOR [options]
jobchain rerun --status STATUS [options]
```

Bare, it re-runs every stage for the selected rows at a new generation.

| Option | Effect |
|---|---|
| `--set COL=VALUE` | Change a value first. Repeatable. Implies regeneration |
| `--stage NAME` | One stage only, at the current generation |
| `--stages A,B` | Those stages, in order, with dependencies between them |
| `--from NAME` | That stage and everything after it |
| `--chain` | Resume chaining from this row. Off by default |
| `--regenerate` | Rebuild scripts even without `--set` |
| `--fresh-handoff` | Start the new generation with an empty handoff file |
| `--force` | Override the attempt cap, an active job, or a completed row |
| `--yes` | Skip the typed confirmation |

### `cancel`

```
jobchain cancel --row SELECTOR
jobchain cancel --status STATUS
```

Cancels every stage of the selected rows, marks them `cancelled`, and leaves
them rerunnable. The chain breaks there; `rerun --chain` resumes it.

| Option | Effect |
|---|---|
| `--stage NAME` | One stage only. Dependents become unsatisfiable |
| `--all` | Every active row in the run, and sets the stop marker |
| `--stop` | Set the stop marker only; work in flight finishes |

### Stopping a run

Cancelling active rows is not by itself enough to stop a run: rows already
queued still execute, and their chaining stages still claim more work. A stop
marker in the run directory closes that gap. The chaining stage checks for it
before claiming, so a stop takes effect at the next chain advance without
having to reach every node.

| Goal | Command |
|---|---|
| Take no new work; let running jobs finish | `jobchain cancel --stop` |
| Stop everything now | `jobchain cancel --all` |
| Resume | `jobchain run --resume` |

`run --resume` clears the marker and relaunches chains up to the configured
width. While the marker is present, `status` says so on its first line, and
`doctor --repair` will not relaunch chains.

### `doctor`

```
jobchain doctor [--repair] [--all]
```

| Option | Effect |
|---|---|
| `--repair` | Reset orphaned rows and relaunch chains to the configured width |
| `--all` | Check every run |
| `--check-fs` | Verify the filesystem supports the claim protocol |

### `logs`

```
jobchain logs [--follow] [--level LEVEL] [--stage NAME] [--all]
```

### `export`

```
jobchain export [-o FILE] [--status STATUS] [--format csv|tsv|json]
```

## Views and their fields

### `run` — the normal path

```
jobchain 0.5   run 'solver-production'

  config      solver.yaml
  params      runs.psv                    240 rows
  schema      inline                      7 fields, 2 row checks, 1 file check
  pipeline    inline                      4 stages, chaining on 'archive'
  scheduler   pbs                         from config
  width       8                           up to 32 jobs queued
  home        /scratch/proj/.jobchain/solver-production

[1/4] normalizing
      240 rows read, 3 normalized, 2 blank and 4 comment lines skipped

[2/4] validating
      238 valid, 2 invalid

[3/4] generating scripts
      [########################################] 952/952   8 workers   12.4s

[4/4] submitting
      row 000001  r001  prep 4411  solve 4412  reduce 4413  archive 4414
      row 000002  r002  prep 4415  solve 4416  reduce 4417  archive 4418
      ... 6 more
      8 chains started, 32 jobs queued

  ⚠  2 rows were NOT submitted — they failed validation

     line  47  r047  threads: 256 is greater than maximum 128
     line 112  r112  input_file: path '/data/omega.h5' does not exist

```

| Field | Meaning |
|---|---|
| `config` | The file passed, recorded as `config.original.yaml` |
| `params`, row count | Lines read before validation |
| `schema`, `pipeline` | `inline`, or the path if separate |
| `scheduler` | Detected or configured, with the client version |
| `width`, queued estimate | Chains, and width × stages, so queue impact is explicit |
| `home` | Where all state for this run lives |

The invalid-row block appears after submission rather than before, so it is
the last thing on screen, and it repeats at the top of every `status` until
the count reaches zero.

Messages state what happened and stop. jobchain does not suggest follow-up
commands: those are documented once, in the command reference, rather than
duplicated across every error and kept in step by hand.

### `status`

```
run 'solver-production'   Nightly production solve over the full parameter sweep
home /scratch/proj/.jobchain/solver-production      started 2026-08-12 22:14

[###################!!!..................] 96/238 (40.3%)
DONE 93   FAILED 3   RUNNING 8   QUEUED 24   PENDING 110   INVALID 2

⚠  2 rows failed validation and were never submitted

ROW     ID    STATUS          STAGE    GEN TRY JOBID  ELAPSED HOST
000094  r094  failed.solve.2  solve    1   1   4478   22m     node18
000095  r095  DONE            archive  1   1   4482   2.4h    node22
000096  r096  DONE            archive  1   1   4486   2.1h    node09
000097  r097  RUNNING         solve    1   1   4487   1.2h    node31
000098  r098  QUEUED          prep     1   1   4491   -       -
```

| Column | Meaning |
|---|---|
| `ROW` | State directory name, assigned in file order |
| `ID` | The `id_field` value |
| `STATUS` | Roll-up status for the row |
| `STAGE` | The stage reached; for a failure, the stage that failed |
| `GEN` | Current generation; rises on each full rerun |
| `TRY` | Attempts claimed so far |
| `JOBID` | Job id of the current stage |
| `ELAPSED` | Time in the current stage, or total for a finished row |
| `HOST` | Execution host, from the row's timeline |

The completion bar shows `#` for succeeded, `!` for failed, `.` for
outstanding.

**Warnings appear above the table**, never buried: invalid rows, chains below
the configured width, and a stale `doctor` on a run that looks stalled.

```
⚠  3 chains live, configured width 8. Chains may have been lost.
```

### `status --metrics --summary-only`

```
[###################!!!..................] 96/238 (40.3%)
DONE 93   FAILED 3   RUNNING 8   QUEUED 24   PENDING 110   INVALID 2

Finished        96 of 238
Failure rate    3.1%
Per stage       prep     mean 8m     median 7m    failures 0
                solve    mean 1.9h   median 1.6h  failures 3
                reduce   mean 24m    median 22m   failures 0
                archive  mean 11m    median 10m   failures 0
Wall elapsed    6.2h
Throughput      15.5 rows/hour
Projected left  9.2h   (assumes throughput holds)
Chains          8 of 8 live
```

| Field | Meaning |
|---|---|
| `Finished` | Rows in a terminal state, over the total |
| `Failure rate` | Failed and cancelled, over finished |
| `Per stage` | Timing over **successful** runs only, so failures do not distort it |
| `Wall elapsed` | First to last recorded event |
| `Throughput` | Finished rows per hour over wall elapsed |
| `Projected left` | Remaining rows at the observed rate. A guide, shown only with evidence |
| `Chains` | Live against configured. The number that reveals a stalled run |

### `show` — a failed row

Sections are chosen by state: a failure leads with the failure.

```
jobchain show --row r094
```

```
row 000094   r094   line 101   generation 1   failed.solve.2

FAILURE
  stage       solve
  exit        2
  job         4478.head on node18
  when        2026-08-11 23:33:02, after 22m 11s
  message     solver: out of memory allocating 48.2 GB

PARAMETERS
  input_file  /data/psi.h5          mode        gpu
  threads     8                     mesh_size   large
  tolerance   1e-08                 output_dir  /scratch/proj/psi

STAGES
  stage    status     job        depends   walltime  ncpus  mem   elapsed  host
  prep     DONE       4477.head  -         00:30:00      2   8gb  8m 33s   node18
  solve    FAILED     4478.head  afterok   16:00:00     32  32gb  22m 11s  node18
  reduce   CANCELLED  4479.head  afterok   01:00:00      8  16gb  -        -
  archive  DONE       4480.head  afterany  02:00:00      1   4gb  4m 02s   node18

HANDOFF
  mesh_file   /scratch/proj/psi/mesh.h5
  cell_count  1841203

PATHS
  state       /scratch/proj/.jobchain/solver-production/rows/000094
  work        /scratch/proj/psi                       412 files, 24.1 GB
  scripts     /scratch/proj/psi/scripts
                01-prep.sh  02-solve.sh  03-reduce.sh  04-archive.sh
  logs        /scratch/proj/.jobchain/solver-production/logs/000094
```

The stage table shows resources **as requested at submission**, beside the
elapsed time actually used. That pairing is what identifies a walltime kill or
a badly sized reservation without opening another view.

| Section | When it appears |
|---|---|
| `FAILURE` | Only for a failed row, and first |
| `PARAMETERS` | Always |
| `STAGES` | When a pipeline is configured |
| `HANDOFF` | When stages emitted values |
| `PATHS` | Always |

A healthy completed row prints `PARAMETERS`, `STAGES`, and `PATHS` only — six
lines rather than forty.

**Output reporting is directory-level throughout.** Work directories are shown
with a file count and total size, never a file listing, because a stage may
produce thousands of files. Script and log paths are shown in full, since
those are individual files to open or resubmit.

### `show --invalid`

```
2 rows failed validation and were never submitted.

LINE  ID    COLUMN       REASON
  47  r047  threads      256 is greater than maximum 128
 112  r112  input_file   path '/data/omega.h5' does not exist

```

Invalid rows have state directories but no scripts, so `rerun --set`
re-validates and, if the row now passes, generates its scripts and queues it.

### `logs`

`logs` shows jobchain's own record of the run: what it validated, generated,
submitted, and observed. It is the run's narrative, in one file per run.

It is **not** a job's own output. Anything a stage printed belongs to the
scheduler, in `<home>/logs/<row>/<stage>.log`, and is reached with
`show --row X --output`.

| Source | What it holds | How to read it |
|---|---|---|
| `<home>/jobchain.log` | jobchain's actions and observations | `jobchain logs` |
| `<home>/logs/<row>/<stage>.log` | The stage's own stdout and stderr | `jobchain show --row X --output` |
| `<home>/rows/<row>/run-N/timeline` | One row's status transitions | `jobchain show --row X --history` |

```
jobchain logs                    recent entries
jobchain logs --follow           tail as the run proceeds
jobchain logs --level warning    only warnings and errors
jobchain logs --stage solve      only entries about that stage
jobchain logs --all              every run, with a run column
```

```
2026-08-11 23:10:44 INFO   row 000094 stage prep DONE after 8m33s on node18
2026-08-11 23:10:44 INFO   row 000094 stage prep wrote /scratch/proj/psi/mesh
                           (18 files, 4.7 GB)
2026-08-11 23:10:52 INFO   row 000094 chained to row 000095
2026-08-11 23:10:53 INFO   row 000095 stage prep submitted as 4481.head
2026-08-11 23:33:02 WARN   row 000094 stage solve FAILED exit 2 on node18
2026-08-11 23:33:03 INFO   row 000094 stage reduce cancelled by dependency
```

### `doctor`

```
run 'solver-production'

chains       3 live, configured width 8              SHORTFALL 5
rows         238 total, 96 finished, 8 active, 134 pending

findings (6)
  [found] row 000101 stage solve: recorded RUNNING but job 4501.head is no
          longer known to the scheduler
  [found] row 000101 stage reduce: QUEUED behind a job that will never complete
  [found] row 000104 stage prep: CLAIMED with no job id; the submitting
          process did not finish
  [found] row 000107: chaining stage 'archive' never ran; the chain ended here
  [found] parameter file runs.psv has changed since setup; running rows
          reflect the original file
  [found] 5 chains short of the configured width

environment
  qsub        /opt/pbs/bin/qsub
  qstat       /opt/pbs/bin/qstat
  qdel        /opt/pbs/bin/qdel
  clock skew  0.4s against the shared filesystem

```

`doctor` exists because a broken chain reports nothing. The run does not fail;
it quietly runs fewer chains, and eventually none, while `status` still shows
a plausible mixture of finished and pending rows.

| Finding | What `--repair` does |
|---|---|
| Job vanished while active | Mark the stage failed; cancel stages queued behind it |
| Claimed with no job id | Release the claim so the row can be taken again |
| Chain ended early | Launch a replacement chain |
| Chains below width | Launch enough to reach it |
| Parameter file changed | Nothing. Reported only |
| Script missing | Nothing. Reported only |

**Repair does not re-queue the rows it marks failed.** A row whose job
vanished may have written partial output, or may fail again for the same
reason. That decision stays explicit:

```
jobchain doctor --repair
jobchain status --status FAILED
jobchain rerun --status FAILED
```

It also cannot tell a slow job from a stuck one: a job the scheduler still
lists as running is healthy as far as `doctor` is concerned. Walltime limits
are the scheduler's responsibility.

Safe to run at any time without `--repair`, which makes it suitable for cron:

```
*/30 * * * * cd /scratch/proj && jobchain doctor --all --repair >> doctor.log 2>&1
```

### `export`

`export` writes the parameter file back out with state columns appended. Every
original column is preserved in its original order, so the result is still a
valid input to the same schema, and the appended columns describe what
happened to each row.

It exists so results can be handed to a spreadsheet, a plotting script, or a
downstream tool without anyone reading jobchain's state directory. It is
generated on demand and never maintained, which is what keeps the run itself
lock-free.

| Appended column | Meaning |
|---|---|
| `status` | Roll-up status, including `failed.validation.<id>` for skipped rows |
| `stage` | The stage reached, or the one that failed |
| `generation` | Current attempt number |
| `attempts` | How many generations have been claimed |
| `elapsed_s` | Seconds from first start to terminal status |
| `work_dir` | The row's working directory |
| `error` | First line of the recorded error, if any |

```
jobchain export -o results.psv
jobchain export --status FAILED -o failures.psv
jobchain export --format json -o results.json
```

```
run_id|input_file|...|status|stage|generation|attempts|elapsed_s|work_dir|error
r001|data/alpha.h5|...|DONE|archive|1|1|8134|/scratch/proj/alpha|
r002|data/beta.h5|...|RUNNING|solve|1|1||/scratch/proj/beta|
r047|data/omega.h5|...|failed.validation.4|||||threads: 256 is greater than maximum 128
```

Every original column, then state columns. Regenerated on demand, so it costs
nothing during the run and needs no lock.

## Correcting rows

Nothing is ever mutated in place. Corrections append, which is what makes them
safe while jobs are running.

| Goal | Command |
|---|---|
| Re-run a failed row unchanged | `jobchain rerun --row r094` |
| Fix a value and re-run | `jobchain rerun --row r094 --set threads=16` |
| Re-run from a stage onward | `jobchain rerun --row r094 --from solve` |
| Re-run one stage | `jobchain rerun --row r094 --stage solve` |
| Re-run every row that failed at a stage | `jobchain rerun --status failed.solve --from solve` |
| Bring an invalid row into the run | `jobchain rerun --row r047 --set threads=64` |
| Resume a stalled chain | `jobchain rerun --row r094 --chain` |
| Outside jobchain entirely | `qsub /scratch/proj/psi/scripts/02-solve.sh` |

`--set` re-validates the new values **before writing anything**, so a rejected
correction leaves the run exactly as it was:

```
error: the revised values for row 000094 do not validate
  threads: 999 is greater than maximum 128 (expected worker threads)
```

A correction takes the row out of circulation with a hold file, rewrites its
parameters and scripts, then raises the generation last. A claimer therefore
sees either the old generation with the old parameters, or the new generation
with the new ones — never a mixture.

### Protection against destroying results

Re-running something that already succeeded is unusual, so it is confirmed in
proportion to what is at risk.

| Rerunning | Confirmation |
|---|---|
| Failed, cancelled, or invalid rows | None. The normal case, with nothing to lose |
| A completed row whose output directories are gone | `--force` |
| A completed row whose output directories still exist | `--force` **and** a typed confirmation |

Whether output still exists is checked, not assumed. If the directories a
completed row wrote to have been removed, the cleanup has already happened and
the rerun cannot destroy anything, so `--force` alone is enough. If they are
still there, the confirmation applies however few rows are selected — one row
is protected exactly as ten are. `--yes` skips the typing for scripted use.

```
$ jobchain rerun --row case_name=somecase --force

row 000042 (somecase) completed successfully at 2026-08-12 23:41:02,
4h 12m across 4 stages. Output directories still exist:

  /scratch/proj/somecase/results          188 files, 18.2 GB
  /scratch/proj/somecase/reduced           12 files,  1.1 GB
  /scratch/proj/somecase/mesh             212 files,  4.8 GB

These are directories jobchain knows about. Stages may write elsewhere.
Re-running executes all 4 stages again and may overwrite them.

Type the row id to confirm:

```

jobchain's own record is never at risk: a rerun creates `run-2/` beside
`run-1/`, and every prior attempt stays readable.

Setting `work_dir: "{row.output_dir}/gen{row.generation}"` namespaces output
per attempt. When that is in effect, a rerun writes to a directory that does
not yet exist, so the check finds nothing at risk and only `--force` is
required.

## Running several runs at once

A run is identified by the `name` in its config. That name is the state
directory, appears in every log line, prefixes every job name, and is how
every command selects a run.

For repeated executions of one config, `name` may carry a template:

```yaml
name: "solver-{date}"          # solver-2026-08-12
name: "sweep-{user}-{time}"
```

### Selection

| Situation | Behavior |
|---|---|
| One run exists | Used automatically |
| Several exist, `--run NAME` given | That one |
| Several exist, `JOBCHAIN_RUN` set | That one |
| Several exist, none specified | **List them and stop.** Never guess |

```
$ jobchain status
error: 3 runs exist; specify one with --run

  NAME                 ROWS  DONE  FAILED  ACTIVE  STARTED
  solver-production     238    96       3       8  22:14
  solver-testing         12    12       0       0  19:02  (complete)
  mesh-study            400    17       0      16  06:41
```

### Per-command behavior

| Command | With several runs |
|---|---|
| `run` | Creates a new run; refuses a name collision |
| `status` | No selection lists runs; `--all` gives per-run summaries |
| `show`, `rerun`, `cancel` | Require a run. **Never operate across runs**, even with `--all` |
| `logs` | Per run; `--all` interleaves with a run column |
| `doctor` | Per run; `--all` checks every run. The form worth running from cron |
| `export` | Per run |

### Isolation

| Resource | How it is isolated |
|---|---|
| State directory | `.jobchain/<name>/` |
| Log file | `.jobchain/<name>/jobchain.log` |
| Setup lock | Per run; two runs may prepare concurrently |
| Row claiming | Per run's own index; never crosses runs |
| Job names | Prefixed with the run name |
| Job environment | `JC_RUN_NAME` set on every job |

One shared resource remains: **the directory a stage class writes scripts to**.
If two runs use the same template over the same parameter file, they write to
the same paths. jobchain records script paths per run and refuses a collision:

```
error: run 'solver-retry' would write scripts where run 'solver-production'
       already has them: /scratch/proj/alpha/scripts
```

The default template includes `{run.name}`, so this only arises when overridden.

### Monitoring several runs

```
$ jobchain status --all

NAME                 ROWS  DONE  FAILED  ACTIVE  CHAINS  THROUGHPUT  ETA
solver-production     238    96       3       8    8/8    15.5/h      9.2h
mesh-study            400    17       0      16   16/16    4.1/h      93h
solver-testing         12    12       0       0      -    complete    -

3 runs, 24 chains active, 1 run with failures
```

Finished runs accumulate. `jobchain status --all --prune-after DAYS` removes
state for runs where every row is terminal and nothing has changed in that
many days. It reports what it would remove and requires `--yes` to proceed.
Nothing is ever removed automatically.

## Completion and notification

When every row reaches a terminal state, jobchain writes `done.json` and runs
the `on_complete` hook.

```json
{
  "run": "solver-production",
  "completion": 2,
  "completed_at": "2026-08-13T04:12:08",
  "first_completed_at": "2026-08-12T23:41:02",
  "reruns_since_first": 6,
  "rows": {"total": 240, "done": 236, "failed": 2, "invalid": 2},
  "elapsed_s": 108364,
  "work_dirs": ["/scratch/proj"]
}
```

Rules that keep it meaningful:

- Written only when nothing is outstanding.
- **Deleted the moment any row leaves a terminal state**, so a rerun removes
  it immediately. Its presence always means "nothing outstanding right now."
- `completion` increments each time completion is reached, distinguishing the
  first from one following reruns.
- `run --force` discards the run directory, so a fresh run starts at 1.
- `completions.log` keeps one line per completion.

The hook runs on each completion with `JC_RUN_NAME`, `JC_COMPLETION`,
`JC_ROWS_DONE`, `JC_ROWS_FAILED`, and `JC_HOME` in its environment, so a
script can notify only on the first completion, or only when failures are zero.

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
| 8 | The compiled helper is missing or failed |
| 9 | The requested operation conflicts with a running job |

**A traceback reaching the terminal always indicates a defect in this tool,
never bad input.** Every expected failure produces a message and one of these
codes.

---

# Part II — Reference

## Complete YAML reference

### Run configuration

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Run name; the state directory under `.jobchain/`. Supports `{date}`, `{time}`, `{user}` |
| `description` | string | — | Free text, shown by `status` |
| `params` | path | required | The delimited parameter file. Relative to this config |
| `schema` | mapping or path | required | Inline schema, or a path to one |
| `pipeline` | mapping or path | — | Inline pipeline, or a path. Omitted means one job per row |
| `width` | int | 1 | Chains advancing concurrently |
| `max_attempts` | int | unlimited | Attempt cap per row. Unset means no cap |
| `max_in_flight` | int | unset | Ceiling on submitted-but-unfinished pipelines |
| `strict` | bool | `false` | Refuse to proceed if any row fails validation |
| `workers` | int | CPU count | Threads for script generation |
| `scheduler` | `pbs` \| `slurm` | `pbs` | Which scheduler scripts are generated for. Never detected |
| `on_complete` | string | — | Command run when the run completes |
| `logging.terminal` | level | `info` | Console verbosity |
| `logging.file` | level | `debug` | File verbosity |
| `logging.file_name` | string | `jobchain.log` | Log file name in the run directory |
| `paths.work_dir` | template | `{run.home}/work/{row.name}` | Per-row working directory |
| `paths.log_dir` | template | `{run.home}/logs` | Scheduler output location |

### Schema

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Schema name, shown in reports |
| `version`, `description` | string | — | Free text |
| `format.delimiter` | char or alias | `,` | `comma`, `tab`, `pipe`, `colon`, `semicolon`, `space`, `whitespace`, or a literal |
| `format.header` | bool | `false` | Whether the first non-comment line names columns |
| `format.comment` | string | `#` | Lines starting with this are ignored |
| `format.quoting` | bool | `false` | Honour quoted fields. Requires a single-character delimiter |
| `format.id_field` | column name | — | Which column identifies a row in reports. Optional; implies `unique: true` |
| `validator_class` | path | — | Module supplying validation instead of `fields` |
| `fields[]` | list | required unless `validator_class` | Column definitions, matched **by position** |
| `row_checks[]` | list | — | Cross-column checks within one row |
| `file_checks[]` | list | — | Cross-row checks over the whole file |

**Field entry**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Column name; becomes `JC_<name>` in the job environment |
| `description` | string | — | Shown in failure messages |
| `type` | string | — | Shorthand check, with its arguments as sibling keys |
| `checks[]` | list of mappings | — | Explicit list when more than one check applies |
| `optional` | bool | `false` | Permits an empty value |
| `default` | any | `null` | Value yielded for an empty optional field |
| `unique` | bool | `false` | Enforce uniqueness, and allow `--row name=value` lookup |
| `python` | string | — | `file.py:NAME` reference to a `Validator` instance |

### Pipeline

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Pipeline name |
| `version`, `description` | string | — | Free text |
| `stage_module` | path | — | Module holding stage classes. Relative to this file |
| `defaults` | mapping | — | Resource defaults applied to every stage |
| `stages[]` | list | required | Ordered stages; order is submission order |

**Stage entry**

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Stage label |
| `uses` | string | derived from `name` | Class implementing this stage |
| `depends` | `afterok` \| `afterany` \| `afternotok` | `afterok` | Dependency on the previous stage. Ignored for the first |
| `chains_next` | bool | last stage | Which stage claims the next row. Must be `afterany` |
| `command` | string | — | For a stage with no class |
| `walltime` | string | — | `HH:MM:SS` |
| `nodes`, `ncpus`, `ngpus` | int | — | Resource counts |
| `mem` | string | — | For example `8gb` |
| `queue`, `account` | string | — | Scheduler queue and charge code |
| `extra_directives` | list | — | Lines passed through verbatim |
| `env` | mapping | — | Extra environment variables for this stage |
| *(class settings)* | — | — | Any key declared in the class's `settings` |

Unrecognized keys are a load-time error naming the stage, so a typo surfaces
before anything is generated.

## Validator reference

### Field checks

| Type | Arguments | Accepts |
|---|---|---|
| `int` | `min`, `max` | Optionally signed digits |
| `float` | `min`, `max`, `allow_nonfinite` | Decimal, optionally exponential |
| `str` | `min_length`, `max_length`, `charset` | Text |
| `bool` | — | `true/false`, `yes/no`, `on/off`, `1/0`, any case |
| `one_of` | `values`, `case_sensitive` | Membership in a fixed set |
| `exact` | `value` | One literal, compared as text |
| `regex` | `pattern`, `ignore_case` | Full-length match |
| `path_exists` | `must_be_file`, `must_be_dir`, `readable` | An existing path |
| `output_path` | `must_not_exist` | A path whose parent exists and is writable |
| `all_of`, `any_of` | `of` | Every, or at least one, child check |

### Two rejections worth knowing

**`int` rejects underscore separators.** `int("1_0")` returns 10 in Python. A
typo would silently become a different number, so a value validates only if it
reads as an integer to a person as well.

**`float` rejects `nan` and `inf`** unless `allow_nonfinite: true`. Both parse
successfully in Python, and neither is a meaningful job parameter; a NaN
reaching a job is far harder to diagnose than a validation failure.

### Normalization performed by checks

| Check | Transformation |
|---|---|
| all | Surrounding whitespace stripped |
| `int`, `float` | Converted to a number, so `007` reaches the job as `7` |
| `bool` | Converted to `1` or `0` |
| `one_of` with `case_sensitive: false` | Returns the member **as spelled in the schema** |
| `path_exists`, `output_path` | `~` and `$VAR` expanded; relative paths made absolute against the parameter file's directory |

### Row checks

| Type | Arguments | Requires |
|---|---|---|
| `required_when` | `when_field`, `equals`, `require_field` | A column set when another holds a value |
| `compare` | `left`, `op`, `right` | A relationship between two columns. `op` is `<  <=  >  >=  ==  !=` |

### File checks

| Type | Arguments | Requires |
|---|---|---|
| `unique` | `fields` | A column, or tuple, to be unique |
| `row_count` | `min`, `max` | The row count in a range |

## Pipeline order

Every value takes the same path from file to job, in a fixed order. Each stage
sees the output of the one before it.

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
  8  NORMALIZE      each check's canonicalization: case folding, path
                    expansion, numeric parsing
  9  FIELD CHECKS   in declaration order; first failure stops that field
                    -> a row failing here skips steps 10-11
 10  ROW CHECKS     relationships between columns, on converted values
 11  FILE CHECKS    uniqueness and counts, over rows that passed 7-10
  ------------------------------------------------------------------ gate
 12  GATE           with strict: true, any failure stops the run
                    otherwise invalid rows are recorded and skipped
  ------------------------------------------------------------------ prepare
 13  ROW STATE      state directory, env fragment, generation, manifest
                    written for every row, valid or not
 14  RENDER         one script per stage per valid row, in parallel
                    resources: pipeline defaults -> stage YAML -> class
 15  VERIFY         each script non-empty and parses as shell
  ------------------------------------------------------------------ run
 16  CLAIM          mkdir run-<gen>; exactly one caller wins
 17  SUBMIT         stages in order, threading dependency job ids
 18  EXECUTE        each stage sources env and handoff, runs, marks status
 19  CHAIN          the chaining stage claims and submits the next row
```

`run --check` stops after 12; `run --no-submit` stops after 15.

Two properties are worth stating plainly. **Step 5 can never change the field
count**, which stops a stray delimiter from shifting every later column. And
**step 8 runs before step 9**, so a check always sees the canonical form of a
value.

## Option behavior in detail

### Field checks, given an empty value

An empty field is a value, not an absence. Unless a field is `optional`, most
checks reject it.

| Check | Given `""` | Given whitespace only | Value passed to the job |
|---|---|---|---|
| `int` | Fails | Fails, after trimming | Python `int` |
| `float` | Fails | Fails | Python `float` |
| `str` | **Passes** unless `min_length` is set | Passes as `""` | The trimmed string |
| `bool` | Fails | Fails | `1` or `0` |
| `one_of` | Fails unless `""` is listed | Fails | The member as spelled in the schema |
| `exact` | Passes only if the literal is `""` | Fails | The raw string |
| `regex` | Passes if the pattern matches `""` | Fails | The raw string |
| `path_exists` | Fails | Fails | Absolute, expanded path |
| `output_path` | **Passes** — the parent of an empty path exists | Passes | Absolute, expanded path |
| `optional: true` | **Passes**, yielding `default` | Passes, yielding `default` | `default`, or `None` |

`str`, `exact` with an empty literal, `regex` with a zero-width pattern, and
`output_path` all accept an empty value without `optional`. If a column must
not be blank, say so: `str` with `min_length: 1`.

`optional` short-circuits: an empty value never reaches the inner check, so
`optional` plus `int(min=1)` accepts empty and rejects `0`.

### Missing and extra columns

| Situation | Result |
|---|---|
| Fewer fields than the schema | Structural failure; field checks skipped |
| More fields than the schema | Structural failure; extras are not dropped |
| Header names disagree | Warning only. Columns match by **position** |
| Header column count differs | Warning naming both counts |
| No data rows | Scans clean unless `row_count` says otherwise |

### Row and file checks, given absent values

| Check | Behavior |
|---|---|
| `required_when` | Does nothing unless `when_field` equals `equals`. `None` and `""` both count as absent |
| `compare` | Skipped if either side is `None`, so a missing optional column is not reported twice. Incomparable types reported plainly |
| `unique` | Compares converted values, so `007` and `7` collide in an `int` column but not a `str` one. Attributed to the **later** row, naming the first |
| `row_count` | Reported against the file, not any row |

### Resource resolution

| Situation | Result |
|---|---|
| Set in pipeline defaults only | Every stage and row uses it |
| Set in a stage block | That stage overrides the default |
| Returned by `resources()` | That row overrides both |
| Returned as an empty dict | YAML values used unchanged |
| Resolved value is `0` or `null` | No directive emitted; site default applies |
| Unrecognized key returned | Load-time error listing valid keys |

### Chaining and dependency

| Situation | Result |
|---|---|
| Chaining stage succeeds | Next row claimed and submitted |
| Chaining stage fails | Next row still claimed; the call is not conditional on exit status |
| Chaining stage cancelled or never runs | Chain stops. `doctor` detects and repairs |
| `afterok` predecessor fails | Successor cancelled by the scheduler |
| `afterany` predecessor fails | Successor runs |
| Script submitted by hand | Runs immediately, marks status, does **not** chain — `JC_CHAIN` is unset |

### Handoff values

| Situation | Result |
|---|---|
| Stage emits a key twice | Last value wins |
| Later stage reads an unset key | Empty string; the script decides what that means |
| Rerun at a new generation | Handoff copied forward from the previous generation |
| `--fresh-handoff` | New generation starts empty |
| Partial rerun with `--from` | Earlier stages' values remain available |

Handoff lives at `run-<gen>/handoff`, not at row level. This matters: if it
were shared across generations, a stage that failed in generation 2 would
leave generation 1's value visible to later stages, and an `afterany`
successor would consume stale data without any error.

## A worked example

One row, from the file to the queue.

**As written:**

```
r002|data/beta.h5 |/scratch/proj/beta |GPU|32|large|1e-8
```

| Step | Result |
|---|---|
| Split | 7 fields |
| Trim | Trailing spaces removed from fields 2 and 3; count still 7 |
| Structure | 7 fields, 7 declared: passes |
| `run_id` | `r002` matches `[A-Za-z0-9_-]+` |
| `input_file` | Expanded to `/scratch/proj/data/beta.h5`, exists, readable |
| `output_dir` | Parent exists and is writable |
| `mode` | `GPU` matched case-insensitively, canonicalized to `gpu` |
| `threads` | `32` → int 32, within 1–128 |
| `mesh_size` | `large` in the permitted set |
| `tolerance` | `1e-8` → float 1e-08, within 0–1 |
| Row checks | `mode == gpu` requires `ngpus`; `ngpus <= threads` holds |
| File checks | `run_id` and `output_dir` unique |

**Row state written:**

```
.jobchain/solver-production/rows/000002/
  meta.json   typed values, line number, index
  env         JC_run_id='r002'  JC_threads='32'  JC_mode='gpu' ...
  gen         1
  manifest    prep - ...01-prep.sh
              solve afterok ...02-solve.sh
              reduce afterok ...03-reduce.sh
              archive afterany ...04-archive.sh
```

**Resources for `solve`,** merging pipeline defaults, stage YAML, and the class:

| Key | Value | Source |
|---|---|---|
| `walltime` | `16:00:00` | class, from `mesh_size: large` |
| `ncpus` | `32` | class, from `threads` |
| `ngpus` | `2` | class, from `mode: gpu` |
| `mem` | `32gb` | stage YAML |
| `queue` | `normal` | pipeline defaults |

**Directives generated:**

```sh
#PBS -N solver-production-solve-000002
#PBS -l select=1:ncpus=32:mem=32gb:ngpus=2
#PBS -l walltime=16:00:00
#PBS -q normal
#PBS -A proj1
#PBS -j oe
#PBS -o /scratch/proj/.jobchain/solver-production/logs/000002/solve.log
```

**Submitted:**

```
qsub                                01-prep.sh     -> 4415.head
qsub -W depend=afterok:4415.head    02-solve.sh    -> 4416.head
qsub -W depend=afterok:4416.head    03-reduce.sh   -> 4417.head
qsub -W depend=afterany:4417.head   04-archive.sh  -> 4418.head
```

## Choosing between similar options

**`run --check`, `run --no-submit`, or `--dry-run`?** `--check` validates and
writes nothing. `--no-submit` validates and generates scripts, so they can be
inspected. `--dry-run` reports what any command would do without doing it.

**`rerun --stage` or `--from`?** `--stage` runs exactly one stage at the
current generation, with no dependency — for re-running a step against
existing data. `--from` runs that stage and everything after it, with
dependencies, which is the usual recovery after fixing a failure.

**`rerun --set` or edit the parameter file?** `--set` changes one row inside
the run, immediately, while other rows keep running. Editing the file changes
nothing already prepared; it requires `run --force`, discarding the run.

**`rerun` or `rerun --chain`?** Without `--chain`, the selected rows run and
stop. With it, the row resumes chaining, pulling in further rows. Use `--chain`
when a cancellation or a lost chain left the run short.

**`strict: true` or the default?** Strict refuses to submit anything if any
row fails, which suits a sweep only meaningful when complete. The default
submits valid rows and reports the rest prominently, which suits independent
units of work.

**`doctor` or `doctor --repair`?** Without `--repair` it only reports and exits
6 if anything was found. With it, affected rows are reset and chains
relaunched, exiting 0.

**`status` or `show`?** `status` is the run; `show` is one row. If the question
is "what is happening", use `status`. If it is "why did this fail", use `show`.

## Task-to-options guide

| Task | Command |
|---|---|
| Check a file before committing | `run config.yaml --check` |
| Prepare without submitting | `run config.yaml --no-submit` |
| Start a run | `run config.yaml` |
| Cautious first run | Set `width: 1`, run, watch, then `run config.yaml` again |
| Submit what was prepared | `run config.yaml` |
| Add chains to a short run | `doctor --repair` |
| Watch progress | `status --watch` |
| See only failures | `status --status FAILED` |
| See rows that never ran | `show --invalid` |
| Diagnose one row | `show --row r094` |
| Find a row by name | `show --row case_name=somecase` |
| Locate output and scripts | `show --row r094 --paths` |
| Read a job's own output | `show --row r094 --output` |
| Estimate completion | `status --metrics` |
| Detect lost chains | `doctor` |
| Repair lost chains | `doctor --repair` |
| Re-run a failure | `rerun --row r094` |
| Fix a value and re-run | `rerun --row r094 --set threads=16` |
| Re-run every solve failure | `rerun --status failed.solve --from solve` |
| Rescue an invalid row | `rerun --row r047 --set threads=64` |
| Stop one row | `cancel --row r094` |
| Stop everything | `cancel --all` |
| Resume after cancelling | `rerun --row r094 --chain` |
| Collect results | `export -o results.psv` |
| Feed another tool | any command with `--json` |
| Watch several runs | `status --all` |
| Nightly health check | `doctor --all --repair` |
| Reproduce a run exactly | `run .jobchain/<name>/config.final.yaml` |

---

# Part III — Developer reference

## Architecture

The split between Python and C follows the split between submit host and
compute node, because compute nodes are not assumed to have an interpreter.

**Python, submit host.** Everything needing YAML, validators, or human output:
configuration, normalization, validation, script generation, submission,
reporting, reconciliation, and correction.

**C, compute node.** Four operations: claim a row, record a status, emit a
handoff value, and submit the next row's manifest. It never parses the
delimited file and never sees the schema, because generation pre-renders each
row's parameters into a shell fragment the job sources directly.

There is **one implementation of the claim protocol.** Python shells out to
the compiled helper rather than reimplementing it, so the two cannot drift.

## Objects and responsibilities

| Object | Created by | Lives for | Responsibility |
|---|---|---|---|
| `RunConfig` | Loading the config plus CLI overrides | The command | Every setting, resolved; written as `config.final.yaml` |
| `Schema` | The schema section or class | The command | Validation of every column, row, and file rule |
| `Pipeline` | The pipeline section | The command | Stage order, dependency types, chaining point, class resolution |
| `JobStage` | `Pipeline`, one per stage | The command | Writing that stage's script for any row |
| `RunContext` | jobchain, once | The command | Run-wide facts: home, scheduler, paths, node binary |
| `RowContext` | jobchain, per row per stage | One `write_script` call | Row paths, and the shell text of calls the script must make |
| `Store` | jobchain | The command | `.jobchain/<run>/`, claims, generations, status |
| `Scheduler` | jobchain | The command | Submission with dependencies, status queries, cancellation |

## The JobStage interface

```python
class JobStage:
    """One stage of a job pipeline.

    A single instance is created per stage and reused for every row, so it
    holds no per-row state; instances are frozen after construction and
    assigning to self raises. write_script is called once for each valid
    row, producing that row's script for this stage.

    Everything about the resulting script is this class's choice: its
    contents, its directives, its template, and the directory it is written
    to. jobchain supplies the row and the contexts, then records the path
    that is returned.
    """

    #: Configuration keys this stage accepts beyond the standard resource
    #: keys, validated against the YAML at load time.
    settings = {}

    def __init__(self, name, config, run):
        self.name = name      # stage label from the YAML
        self.config = config  # this stage's block, merged over defaults,
                              # as an immutable mapping
        self.run = run        # RunContext

    def resources(self, row) -> dict:
        """Scheduler parameters for this row, merged over the YAML block."""
        return {}

    def output_dir(self, row) -> str:
        """Directory this stage's script is written to for this row."""
        return self.run.work_dir(row)

    def write_script(self, row, ctx) -> str:
        """Write this row's script for this stage; return its absolute path."""
        raise NotImplementedError
```

## The context objects

Both are **created by jobchain**, in Python, on the submit host, before
`write_script` is called. Neither is created by the scheduler, and neither
exists at job time. They carry facts a stage class cannot know, and hand back
ready-made shell text so a stage author never reconstructs a helper invocation.

### `RunContext`

| Attribute | Example |
|---|---|
| `run.name` | `solver-production` |
| `run.home` | `/scratch/proj/.jobchain/solver-production` |
| `run.scheduler` | `pbs` |
| `run.node_binary` | `/shared/apps/jobchain/bin/jobchain-node` |
| `run.log_dir` | `.../solver-production/logs` |
| `run.work_dir(row)` | Expands the configured template for that row |

### `RowContext`

| Attribute | Example |
|---|---|
| `ctx.row_name` | `000123` |
| `ctx.stage` | `solve` |
| `ctx.row_dir` | `.../rows/000123` |
| `ctx.run_dir` | `.../rows/000123/run-1` |
| `ctx.env_file` | `.../rows/000123/env` |
| `ctx.handoff` | `.../rows/000123/run-1/handoff` |
| `ctx.work_dir` | `/scratch/proj/beta` |
| `ctx.log_dir` | `.../logs/000123` |
| `ctx.script_path` | Where `ctx.write` places the script |

### Methods returning shell text

**`ctx.directives(resources)`**

```sh
#PBS -N solver-production-solve-000123
#PBS -l select=1:ncpus=32:mem=32gb:ngpus=2
#PBS -l walltime=16:00:00
#PBS -q normal
#PBS -j oe
#PBS -o /scratch/proj/.jobchain/solver-production/logs/000123/solve.log
```

**`ctx.preamble()`**

```sh
JC_HOME="/scratch/proj/.jobchain/solver-production"
JC_RUN_NAME="solver-production"
JC_ROW="000123"
JC_RUN="$JC_HOME/rows/000123/run-1"
JC_NODE="/shared/apps/jobchain/bin/jobchain-node"
export JC_HOME JC_RUN_NAME JC_ROW JC_RUN JC_NODE

. "$JC_HOME/rows/000123/env"
"$JC_NODE" mark --run "$JC_RUN" --stage solve --status RUNNING \
           --jobid "${PBS_JOBID:-}"
```

**`ctx.emit(key, value)`**

```sh
"$JC_NODE" emit --run "$JC_RUN" result='/scratch/proj/beta/result.h5'
```

**`ctx.epilogue()`** — status, and on the chaining stage the guarded advance:

```sh
if [ "$rc" -eq 0 ]; then
    "$JC_NODE" mark --run "$JC_RUN" --stage solve --status DONE
else
    "$JC_NODE" mark --run "$JC_RUN" --stage solve --status FAILED \
               --error "exit status $rc"
fi
```

**`ctx.write(text)`** writes to `ctx.script_path`, makes it executable, and
returns the path.

The row is a plain mapping of validated, typed values: `row["threads"]` is
`32`, `row["mode"]` is `"gpu"`, `row["input_file"]` is absolute.

## The claim protocol

Each row directory holds a generation number. A row is claimable when
`run-<gen>` does not exist, and claiming it means creating that directory:

```c
mkdir(rows/000123/run-2)
```

`mkdir` either creates the directory or fails with `EEXIST`, and NFS
guarantees the server decides which. Exactly one caller can win, however many
nodes try at once. There is no lock, nothing to time out, and nothing to
recover if a node dies mid-claim.

Three consequences follow. **Retrying is raising a number**, so previous
attempts survive intact. **A dead claimer does not release its row** — correct,
because automatic release could run the same parameters twice; recovery is
deliberate, through `doctor`. **Editing is safe during a run**, because a
`hold` file excludes a row from claiming while its parameters are rewritten.

`jobchain doctor --check-fs` verifies these properties on the actual
filesystem: that `mkdir` succeeds, that a repeat fails with `EEXIST`, and that
an atomic write reads back correctly.

## Why the node helper is compiled

The compute-node helper could be a shell script. It is worth stating why it is
not, because the reasoning also defines when a shell version would be
acceptable.

**Correctness is not the issue.** Every primitive the protocol needs exists in
POSIX shell. `mkdir` is the same system call and equally atomic, so claiming
works. `>>` opens with `O_APPEND`, so short appends from concurrent jobs do
not interleave. `printf > tmp && mv tmp target` gives an atomic rename.

Two things are genuinely lost:

**No `fsync`.** Shell cannot force data to disk before the rename, so a node
crash inside the write window can leave an empty file where the compiled
version would leave either the old content or the new. Rare, and only under
node failure, but it weakens the guarantee the state format relies on.

**Claiming becomes a process-spawn loop.** Claiming walks the row index in
order, attempting `mkdir` until one succeeds. Compiled, that is one system
call per row. In shell, each attempt is a `fork` and `exec` of `/bin/mkdir`.

| Rows already claimed | Compiled | Shell |
|---|---|---|
| 100 | under 10 ms | roughly 0.2 s |
| 1,000 | under 50 ms | roughly 2 s |
| 10,000 | roughly 0.2 s | roughly 20 s |

Late in a large run every chaining stage pays that walk, on node time, on
every chain. That is the deciding factor.

**What shell buys** is real and worth weighing: no compiler on the submit
host, no ABI or architecture mismatch across heterogeneous node images, no
static-linking decision, and a helper any user can read and modify.

**The position taken here.** The compiled helper is the default. A shell
implementation of the same protocol ships alongside it as a drop-in
replacement, selected with `JOBCHAIN_NODE`, documented as suitable for runs up
to roughly a thousand rows and for sites that cannot compile. Both are
exercised by the same conformance tests, which also stops the protocol
drifting between them.

A cursor file recording where the last claim succeeded would flatten the scan
for both implementations and make shell viable at any size. It is listed in
[Future work](#future-work) rather than adopted here, because it adds a second
piece of shared mutable state; if added, it must be advisory only, re-derived
whenever it proves wrong, and never authoritative.

## On-disk state

```
/scratch/proj/
├── runs.psv                        input, never modified
├── solver.yaml
├── stages.py
├── .jobchain/
│   └── solver-production/          this execution only
│       ├── config.original.yaml    exactly what was passed
│       ├── config.final.yaml       effective configuration
│       ├── jobchain.log            full-detail log
│       ├── params.normalized       the copy actually consumed
│       ├── scan_report.json        validation result
│       ├── render_report.json      every script written
│       ├── rows.idx                row names in file order
│       ├── events.log              append-only global log
│       ├── done.json               present only when nothing is outstanding
│       ├── completions.log         one line per completion
│       ├── logs/000123/            scheduler output per row
│       └── rows/000123/
│           ├── meta.json           identity and typed parameters
│           ├── env                 JC_<column> fragment
│           ├── gen                 current generation
│           ├── manifest            stage, depends, script path
│           ├── hold                present only during an edit
│           └── run-1/
│               ├── claim           host, pid, time
│               ├── status          roll-up for the row
│               ├── status.solve    per-stage status
│               ├── jobid.solve     per-stage job id
│               ├── handoff         values emitted this generation
│               └── timeline        append-only history
└── beta/scripts/                   wherever the stage class chose
    ├── 01-prep.sh
    ├── 02-solve.sh
    ├── 03-reduce.sh
    └── 04-archive.sh
```

Each attribute is a separate small file rather than one document, so a partial
write cannot corrupt an unrelated attribute and the state stays legible with
`cat` on a node. Status files are replaced by rename, so a reader sees the old
value or the new one, never a truncated word. Timeline and event entries are
single short `O_APPEND` writes, which do not interleave between processes.

## Dependency submission

The manifest exists so the compute-node side needs no YAML parser and no
knowledge of the pipeline: it reads three columns and submits.

```
prep     -         /scratch/proj/beta/scripts/01-prep.sh
solve    afterok   /scratch/proj/beta/scripts/02-solve.sh
reduce   afterok   /scratch/proj/beta/scripts/03-reduce.sh
archive  afterany  /scratch/proj/beta/scripts/04-archive.sh
```

| Scheduler | Dependency argument |
|---|---|
| PBS Professional | `-W depend=afterok:4411.head` |
| Slurm | `--dependency=afterok:4411` |

If a submission mid-pipeline is rejected, the already-submitted stages are
cancelled and the row is marked failed. A partially submitted pipeline is never
left queued, because its later stages would wait on a dependency that can never
be satisfied.

## Chaining

The last stage claims the next row and submits its whole pipeline. The call is
not conditional on exit status, so a pipeline in which every stage failed still
advances the chain.

```sh
if [ "${JC_CHAIN:-0}" = "1" ]; then
    "$JC_NODE" submit --home "$JC_HOME" --next
fi
```

`JC_CHAIN` is exported by jobchain when it submits, and unset for a bare
`qsub`, so a manual rerun records its status and stops.

**The residual risk.** `afterany` fires when the previous job *terminates*. It
does not fire when the last stage never runs at all — a wholesale
cancellation, a rejected mid-pipeline submission, or a node dying before it
started. In those cases the chain stops silently, and `doctor` is the only
thing that detects it. This is why `doctor` is load-bearing rather than
optional, and why `status` warns when chains are below the configured width.

## Logging

Everything of consequence is logged twice: to the terminal at a level suited
to watching, and to `<home>/jobchain.log` at a higher level suited to
diagnosis afterwards.

| Level | Content |
|---|---|
| `error` | Failures that stop work |
| `warning` | Invalid rows, attempt caps, scheduler oddities, lost chains |
| `info` | Phase transitions, submissions, status changes, chain events |
| `debug` | Script paths, resource merges, per-row claims |
| `trace` | Every file write, every subprocess invocation |

| Event | Level | Example |
|---|---|---|
| Validation failure | warning | `row 47 (r047) invalid: threads: 256 is greater than maximum 128` |
| Validation summary | info | `238 of 240 rows valid; 2 recorded as invalid` |
| Generation start | info | `generating 952 scripts across 8 workers` |
| Script written | debug | `wrote .../02-solve.sh (row 000123, stage solve)` |
| Resource merge | debug | `stage solve row 000123: walltime 16:00:00 (class over yaml)` |
| Row claimed | debug | `claimed row 000123 generation 1` |
| Job submitted | info | `row 000123 stage solve submitted as 4412 (depends afterok:4411)` |
| Submission rejected | error | `row 000123 stage reduce rejected: queue limit; cancelling 4411, 4412` |
| Status change | info | `row 000123 stage solve DONE after 1.2h on node31` |
| Output written | info | `row 000123 stage solve wrote /scratch/proj/beta/results (188 files, 18.2 GB)` |
| Chain advance | info | `row 000123 chained to row 000124` |
| Chain exhausted | info | `row 000238 found no further rows; chain ending` |
| Rerun | info | `row 000123 re-queued at generation 2` |
| Correction | info | `row 000123 revised: threads 16 -> 64; 4 scripts regenerated` |
| Completion | info | `run complete (completion 2): 236 done, 2 failed, 2 invalid` |

**Output is reported by directory, never by file.** A stage producing
thousands of files logs its top-level output directories with counts and total
size. Directory scanning is depth-limited to those roots so a deep tree cannot
delay a message, and an unreadable or slow directory reports size unknown
rather than blocking.

Status changes made by jobs are written by the C helper into the row's
timeline; jobchain folds them into the log when it next reads state, so the
log is complete even though jobs never open it.

Log files are per run, so concurrent executions never interleave.

## Parallel script generation

With 238 rows and 4 stages, generation writes 952 scripts. The work is
embarrassingly parallel — no script depends on another — so it runs across a
thread pool defaulting to the CPU count and settable with `workers` or
`--workers`.

Threads are safe unconditionally because `JobStage` instances are frozen: a
class physically cannot cache into `self`, so there is no shared mutable state
to race on.

**All generation completes before the lock is released.** Submission never
begins against a partially rendered run.

Progress is a bar rather than per-script messages, because 952 lines of "wrote
script" is noise. The bar appears when the terminal is interactive; otherwise
a periodic percentage line is logged. Individual paths still go to the log file
at debug level.

Failures are collected rather than aborting on the first: a stage class raising
for 12 rows reports all 12, with row and stage named, and the run stops before
submitting anything.

## Module layout

Dependencies run one way, downward.

```
core ─▶ schema ─▶ parse ─▶ config ─▶ pipeline ─▶ store ─▶ scheduler
                                                            │
                                              operations ◀──┘
                                                    │
                                          report ◀──┘
                                             │
                                            cli
```

| Module | Responsibility |
|---|---|
| `core.py` | Exit codes, exceptions, logging |
| `schema.py` | Validators and the Field/Schema model built from them |
| `parse.py` | Normalization, then the three-tier scan |
| `config.py` | Config merge, capture, template expansion |
| `pipeline.py` | Stage definitions, class resolution, the `JobStage` base |
| `store.py` | Row state, generations, claims, run discovery |
| `scheduler.py` | Submission with dependencies, queries, directives, rendering |
| `operations.py` | run, rerun, cancel, doctor |
| `report.py` | status, show, metrics, export |
| `cli.py` | Argument parsing, orchestration, exit-code mapping |

`cli.py` is the only module that parses arguments or prints for a person, and
the only place an exception becomes an exit code — which is what upholds the
no-traceback rule.

The C helper is a single translation unit in four sections: utilities, state,
claiming, and the front end.

## Testing plan

```
./run_tests.sh                 build, test, coverage, static analysis
./run_tests.sh --fast          skip coverage and the sanitizer build
./run_tests.sh test_node.py    one module
```

The helper is rebuilt first, so tests cannot pass against a stale binary, and
by default it is built with AddressSanitizer and UndefinedBehaviorSanitizer.

**Targets.** At least 90% branch coverage on the Python package, reported per
module in the README. Every command exercised end to end against a stub
scheduler.

**What the suite must guarantee**

- **One row, one winner.** Dozens of simultaneous claimers take distinct rows;
  many claimers against one row produce exactly one winner.
- **Correction during a run.** Rows are corrected and re-queued while claimers
  work, asserting no row executes twice.
- **The field-count invariant.** Normalization never changes a line's field
  count, across generated inputs.
- **Crash safety.** A helper killed mid-write leaves the old status or the new
  one, never a partial file.
- **The chain survives failures.** A pipeline in which every stage fails still
  advances the chain.
- **Dependency threading.** Submitted arguments carry the previous job's id,
  for both schedulers.
- **Partial submission rollback.** A rejection mid-pipeline cancels what was
  already submitted.
- **Handoff isolation.** A stage failing in generation 2 does not expose
  generation 1's value to a later stage.
- **Stage classes are frozen.** Assigning to `self` after construction raises.
- **Run isolation.** Two runs over the same parameter file do not interact;
  claiming, logs, and state stay separate.
- **Generated scripts are valid POSIX shell**, verified with `sh -n`, and
  derive no path from `$0`.
- **Guards fire.** Starting a running run, re-running an active row, and
  re-running a completed row without `--force` all refuse.
- **Documentation matches the code.** The file tree, command list, and version
  are checked against reality.

**Static analysis.** C under `-Wall -Wextra -Werror -Wshadow -Wconversion
-Wstrict-prototypes -std=c99 -pedantic`, plus the suite under sanitizers.
Python under `ruff` and `mypy`, both clean.

## Assumptions and limitations

**Assumptions**

- Compute nodes can call `qsub` or `sbatch`. The chain submits its successor
  from inside a running job; sites that forbid this need the poller in
  [Future work](#future-work).
- The run directory and helper are on storage visible to both submit host and
  compute nodes, and `mkdir` is atomic there.
- Rows are independent and may execute in any order.
- Stage classes are pure functions of `(row, ctx)`, which the frozen-instance
  rule enforces.

**Limitations**

- **Stages cannot compute their own resources from earlier output.** Resources
  are fixed at submit time. A stage may branch internally on handoff values but
  cannot change its own reservation.
- **Path checks run on the submit host.** A compute node may mount storage
  differently, so a pass is strong evidence rather than a guarantee.
- **Scripts carry one scheduler's directives**, fixed at generation. Switching
  requires regenerating.
- **jobchain cannot protect a stage's output files.** Re-running writes to the
  same paths unless `{row.generation}` is in the work directory template.
- **Claiming scans rows in order**, so cost grows with rows examined. Past
  roughly 10,000 rows a cached index would be worth adding.
- **`doctor` is load-bearing** for chain continuity in the cases `afterany`
  cannot cover, and only helps if it is run.
- **Timestamps are local wall clock** on the execution host; badly skewed
  clocks produce misleading elapsed times.

## Future work

- `--batch-size K`: drain K rows per job, to amortize queue latency when
  per-row work is short.
- Queue-limit introspection before submitting, so an oversized width warns
  rather than being rejected.
- A cached claim index for very large parameter files.
- A submit-host poller for sites that forbid submission from compute nodes.
- Per-stage retry policy, so a flaky stage can retry itself before failing the
  row.
- An explicit `class:` override accepting a module path, for stages whose
  implementation lives outside `stage_module`.
- Structured output records, if handoff values ever need to be machine-read
  rather than sourced.
- An advisory claim cursor, to flatten the index scan for very large runs and
  make the shell helper viable at any size.

## Design decisions

| Question | Decision | Rationale |
|---|---|---|
| Where does state live? | A state directory, not the parameter file | Keeps claiming lock-free and makes mid-run correction safe |
| How are rows claimed? | `mkdir` of the generation directory | Atomic on NFS; no lock, no timeout, no stale-lock recovery |
| Are scripts tied to each other? | No | Any stage is independently resubmittable |
| Where do dependencies live? | In the submit arguments | Same reason |
| How is a stage's class chosen? | `uses`, defaulting to the name | Names stay free; two stages may share a class |
| Can a stage cache state? | No; instances are frozen | Makes threaded generation safe by construction |
| Which stage chains? | The last, and it must be `afterany` | Bounds concurrency to the configured width |
| Does chaining depend on success? | No | One bad row must not stall the run |
| Is validation optional? | No, it always runs | The `--check` flag previews it |
| What happens to invalid rows? | Recorded, skipped, no scripts | Lets them be corrected into a running run |
| Strict or permissive by default? | Permissive, with prominent reporting | Rows are independent units; blocking 238 for 2 is rarely wanted |
| Are handoff values typed? | No | Scripts are the developer's; jobchain only runs them |
| Where does handoff live? | Per generation | Stops a failed stage exposing a previous generation's value |
| How are runs isolated? | By name, under `.jobchain/` | Concurrent runs never interact |
| How much does script verification enforce? | Non-empty and parses as shell | Developers add their own |
| How is output reported? | Directories with counts, never file lists | Stages may produce thousands of files |
| Are `init` and `start` separate? | No; `run` is state-aware | One entry point, fewer names to remember |
| Do messages suggest commands? | No | Hint text across dozens of messages is a maintenance burden and drifts out of step |
| Is the scheduler detected? | No; PBS by default | Detection guesses; a wrong guess produces scripts whose directives are ignored |
| Is there an attempt cap by default? | No | A cap is a policy, not a default; set `max_attempts` to impose one |
| How is a run stopped? | A stop marker checked before claiming | Cancelling jobs alone leaves queued work still chaining |
| Is the helper compiled? | Yes, with a shell fallback | Claiming in shell costs a process spawn per row examined |

## Changelog

### 0.5 (proposed)

- Multi-stage pipelines: one row produces N dependent jobs, chained by
  scheduler dependencies.
- `JobStage` classes generate submit scripts; frozen instances, declared
  settings, class resolution via `uses`.
- Single-file configuration carrying schema, pipeline, and run parameters,
  with `config.original.yaml` and `config.final.yaml` captured per run.
- Run isolation under `.jobchain/<name>/`, with multi-run selection,
  monitoring, and collision detection.
- Command surface reduced to `run`, `status`, `show`, `rerun`, `cancel`,
  `doctor`, plus `logs` and `export`. `run` is state-aware; `init`, `start`,
  `validate`, `explain`, `retry`, `revise`, `plan`, `metrics`, `set`, and
  `reset` are gone or folded in.
- Permissive validation by default, with invalid rows recorded, skipped, and
  reported prominently until resolved.
- Unique-column row lookup: `--row case_name=somecase`.
- Per-generation handoff files.
- Parallel script generation with a progress bar.
- Completion detection: `done.json`, `completions.log`, and an `on_complete`
  hook.
- Tiered confirmations protecting completed rows and existing runs.
- Directory-level output reporting throughout.
- Stopping a run: `cancel --stop`, `cancel --all`, and `run --resume`.
- No hint text in messages; no suggested-command blocks in views.
- Defaults changed: scheduler is `pbs` and never detected, `format.header` is
  `false`, `max_attempts` is unlimited, and `confirm_threshold` is removed in
  favour of always confirming a rerun over existing output.

### 0.4

Condensed the source layout: the C helper became one translation unit, and the
Python package went from twelve modules to eight.

### 0.3

Documentation completeness: scope, project structure, configuration, pipeline
order, and per-option edge-case behavior.

### 0.2

Reduced the command surface from sixteen commands to thirteen.

### 0.1

First release: schema-driven validation, lock-free claiming, self-chaining
execution for PBS Professional and Slurm, mid-run correction, and
reconciliation.
