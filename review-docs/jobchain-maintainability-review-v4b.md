# jobchain 0.5-v4b — Extreme Maintainability & Extensibility Review

**Triage note (0.6):** this review's 52 findings (M-01–M-52) have not been
systematically triaged against the current codebase; treat each as
unconfirmed until checked. Three have been spot-checked and addressed as of
`0.6`:
- **M-28** (version identity duplicated across the Python/C/shell layers) —
  fixed; see `CHANGELOG.md`'s `0.6` entry.
- **M-37** (checked-in `MagicMock/` directory of test-mock debris) — fixed;
  the directory is deleted and its root cause (an unconfigured mock leaking
  into `configure_logging`) closed in `tests/test_cli_unit.py`.
- **M-40** (checked-in compiled `bin/jobchain-node` binary) — fixed;
  untracked from git and added to `.gitignore`.

The remaining findings (module/responsibility boundaries, scheduler
abstraction, typed domain objects, and the rest) are unreviewed by this
pass. This review otherwise stands as originally written below.

## Scope

This review examines `jobchain-0.5-v4b.zip` specifically for fundamental software-engineering practices that affect whether a human can understand, maintain, debug, refactor, and extend the package safely.

The review intentionally does **not** propose fixes. Findings describe the observed design, why it creates maintenance/extensibility risk, and where the behavior is located.

The review focuses on:

- module and responsibility boundaries
- coupling and dependency direction
- API/interface design
- state representation
- data contracts
- extensibility mechanisms
- duplication and single-source-of-truth problems
- complexity and cognitive load
- error-handling structure
- testing architecture as a maintenance concern
- repository/build/install hygiene
- documentation/specification drift risk

It does not attempt to classify every correctness, performance, or security issue unless the issue also materially increases maintenance difficulty.

---

## Executive assessment

The package has substantial documentation, explicit comments, typed data structures in many places, a reasonably clear conceptual architecture, and deliberate attempts to centralize behavior. Those qualities make individual pieces understandable.

The primary maintainability problem is that the implementation has grown into several very large, highly coupled modules while still relying heavily on loosely typed dictionaries, stringly typed state, filesystem conventions, private cross-module helpers, dynamic imports, and duplicated protocol definitions. The result is an architecture where understanding a feature often requires reading several large files and tracing implicit contracts between them.

The largest structural concerns are:

1. **Several modules are effectively subsystems rather than modules.** `operations.py`, `schema.py`, `cli.py`, `store.py`, and `scheduler.py` each contain multiple responsibilities that would normally form separate maintenance boundaries.
2. **The scheduler abstraction is not actually polymorphic.** PBS and Slurm behavior is encoded through conditionals in a single class, and the C helper independently contains another scheduler implementation.
3. **The Python/C boundary duplicates the state/protocol model.** Constants, commands, file names, exit codes, environment variables, and filesystem semantics are represented independently in two languages.
4. **Core APIs are heavily dictionary/string based.** Important contracts are encoded as keys such as `"status"`, `"jobid"`, `"depends"`, `"extra_directives"`, `"_position"`, etc. rather than strongly represented domain objects.
5. **Internal/private APIs cross module boundaries.** Multiple modules import names beginning with `_`, making refactoring module internals difficult because those internals are effectively part of the dependency graph.
6. **The command layer and orchestration layer are both very large.** A change to a command frequently requires navigating large procedural functions rather than composing smaller application services.
7. **The test suite mirrors these implementation details.** Tests directly import many private functions and contain very large test modules, which increases refactoring cost and makes internal restructuring harder.
8. **The repository contains generated/runtime artifacts.** The checked-in `MagicMock/` tree and compiled `bin/jobchain-node` are examples of repository state that is not clearly separated from source state.
9. **Version and protocol identity are duplicated.** The Python package reports `0.5-v4b` while the C helper embeds `0.5`, and protocol constants are repeated across layers.
10. **The amount of written specification is itself a maintenance surface.** README, DESIGN, CHANGELOG, BUGFIXES, extensive comments, examples, tests, Python implementation, C implementation, and shell wrappers all describe overlapping behavior.

The overall effect is not that the code is impossible to extend. Rather, extensions are likely to become increasingly expensive because a human must preserve many implicit relationships simultaneously.

---

# Findings

## A. Architecture and responsibility boundaries

### M-01 — `operations.py` is a subsystem-sized orchestration module

**Location:** `jobchain/operations.py` — 1,268 lines.

`operations.py` contains preparation, validation, run continuation, script generation, submission, rerun planning, rerun execution, cancellation, completion checking, doctor/diagnostic behavior, persistence interaction, and several data structures.

The largest functions include:

- `_prepare_fresh()` — approximately 80 lines
- `_generate_scripts()` — approximately 64 lines
- `execute_rerun()` — approximately 53 lines
- `check_completion()` — approximately 52 lines
- `doctor()` — approximately 107 lines
- `run()` — approximately 10 parameters and substantial branching

**Why this is a maintenance problem**

This is a high-centrality module. A human changing one lifecycle behavior must understand preparation, state persistence, script generation, scheduling, and error handling in the same file. Changes that should conceptually affect one responsibility are exposed to unrelated implementation details.

The large size also makes local reasoning harder. A function can look correct while relying on invariants established several hundred lines away.

The module is effectively an application-service layer, persistence client, scheduler coordinator, repair engine, and diagnostics engine at once. That makes it difficult to establish narrow ownership of behavior.

**Extension impact**

Adding a new lifecycle operation, persistence behavior, scheduler behavior, or rerun rule risks increasing this module rather than creating an isolated extension point.

---

### M-02 — `schema.py` combines multiple independent abstractions

**Location:** `jobchain/schema.py` — 1,210 lines.

The module contains:

- primitive validators
- composite validators
- row validators
- file validators
- `Field`
- `Schema`
- Python schema loading
- YAML schema loading
- dynamic module loading
- validator construction
- schema-class construction
- path anchoring
- validator registries

This is several distinct layers of the system in one file.

**Why this is a maintenance problem**

A change to validation semantics, YAML parsing, dynamic plugin loading, filesystem path behavior, or schema object modeling all takes place in the same maintenance boundary.

The file therefore has a large conceptual surface area even when individual classes are small.

The loader also knows the constructors of all validator types, so the declarative format and implementation class registry are tightly coupled.

**Extension impact**

Adding a validator, changing YAML semantics, changing schema composition, or changing plugin loading all increases the risk of accidentally affecting unrelated parts of the schema subsystem.

---

### M-03 — `cli.py` is both a command dispatcher and a large implementation layer

**Location:** `jobchain/cli.py` — 1,047 lines.

`build_parser()` alone is approximately 143 lines. Several command handlers are also large:

- `cmd_run()`
- `cmd_show()`
- `cmd_rerun()`
- `_prune_runs()`
- `main()`

The file contains argument parsing, command-specific option interpretation, logging setup, filesystem discovery, operation invocation, rendering, error conversion, and command behavior.

**Why this is a maintenance problem**

A CLI should ideally provide a relatively thin boundary around application operations. Here, the command layer contains substantial behavior of its own.

This creates two places where business behavior can exist: `operations.py` and `cli.py`.

A human changing a command must determine whether the behavior belongs in the CLI handler or an operation function. That ambiguity tends to produce command-specific logic that becomes difficult to reuse or test outside the CLI.

**Extension impact**

New commands and new options are likely to increase a central file rather than adding isolated command modules.

---

### M-04 — `store.py` is simultaneously persistence layer, state model, filesystem protocol, locking layer, node-helper client, and reporting utility

**Location:** `jobchain/store.py` — 860 lines.

The module includes:

- `StageState`
- `RunState`
- `RowState`
- run discovery
- path generation
- run creation/destruction
- configuration persistence
- locking
- stop/resume state
- row creation
- manifests
- indexes
- holds
- generation management
- handoff state
- row loading
- row claiming
- stage marking
- events
- self-tests
- node helper invocation
- summary calculation
- environment rendering
- shell quoting
- handoff parsing

**Why this is a maintenance problem**

The filesystem representation is effectively a database, but its schema, locking protocol, row lifecycle, serialization, and external-helper protocol are all implemented in one class/module.

That means changes to the physical on-disk layout are likely to have effects on unrelated operations.

It also makes the `Store` class a high-risk dependency for nearly every feature.

**Extension impact**

Any new persistent state, alternate persistence backend, migration behavior, or state query risks expanding the same module.

---

### M-05 — The C helper is a second subsystem-sized implementation

**Location:** `src/jobchain-node.c` — approximately 1,050 lines.

The entire compute-node helper is a single translation unit containing utilities, persistence operations, claiming, submission, state updates, self-testing, argument parsing, and command dispatch.

**Why this is a maintenance problem**

The C implementation has its own architectural surface independent of the Python package.

A human debugging the complete system must understand both the Python store protocol and this C implementation. There is no source-level module boundary inside the C helper corresponding to the major responsibilities.

**Extension impact**

New node-side operations or scheduler behavior increase an already broad translation unit and create another place where system-wide semantics must be maintained.

---

## B. Abstraction and coupling problems

### M-06 — Scheduler support is conditional logic rather than a real scheduler abstraction

**Location:** `jobchain/scheduler.py`, especially `Scheduler.__init__()`, `submit()`, `job_state()`, `_pbs_job_state()`, `_slurm_job_state()`, and `cancel()`.

The class stores a `kind` string and repeatedly branches between PBS and Slurm.

Examples include:

- selecting `qsub` vs. `sbatch`
- selecting `#PBS` vs. `#SBATCH`
- different dependency syntax
- different environment export syntax
- different job-ID parsing
- different status querying
- different cancellation commands

The C helper separately repeats scheduler selection using environment variables and hard-coded command strings.

**Why this is a maintenance problem**

The abstraction boundary is nominal rather than structural. A scheduler is not an object with a scheduler-specific implementation; it is a mode inside one object.

Adding another scheduler would require editing several unrelated conditional branches.

**Extension impact**

A new scheduler is likely to require coordinated changes in:

- the Python scheduler class
- script directive generation
- job submission
- job state interpretation
- cancellation
- C helper submission
- configuration validation
- tests
- documentation

This is a strong indicator that the current extension mechanism does not isolate scheduler-specific behavior.

---

### M-07 — Python and C independently implement the same scheduler protocol

**Locations:**

- `jobchain/scheduler.py`
- `jobchain/store.py`
- `src/jobchain-node.c`

The Python scheduler knows PBS/Slurm commands, dependency formats, environment export behavior, and job state values.

The C helper independently knows:

- `qsub`
- `sbatch`
- `-v`
- `--export=ALL,`
- `-W depend=`
- `--dependency=`
- `JC_SCHEDULER`
- `JOBCHAIN_SCHEDULER`

**Why this is a maintenance problem**

There is no single implementation of scheduler semantics.

A scheduler behavior can therefore be changed correctly in Python while remaining inconsistent in C.

The comments explicitly describe the C implementation as matching the Python front end, which means humans are responsible for preserving that relationship manually.

**Extension impact**

Every scheduler change has a cross-language synchronization cost.

---

### M-08 — Private implementation functions are imported across module boundaries

Examples include:

- `pipeline.py` imports `_import_module` from `schema.py`
- `scheduler.py` imports `_shell_quote` from `store.py`
- `operations.py` imports `_scan_row` from `parse.py`
- `operations.py` imports other private helpers from `parse.py`
- tests import large numbers of private functions

**Why this is a maintenance problem**

A leading underscore convention normally indicates that an implementation detail may change without preserving external compatibility.

Here, private names are nevertheless part of the internal dependency graph.

That means moving or renaming an apparently local helper can break unrelated modules.

More importantly, the ownership of the helper becomes unclear. For example, scheduler code depending on a private store helper means scheduler behavior is coupled to the store implementation.

**Extension impact**

Refactoring a module becomes substantially harder because its private functions are not actually private.

---

### M-09 — Domain concepts cross layers as primitive strings and dictionaries

Important concepts are frequently represented as:

- `Dict[str, Any]`
- `Dict[str, str]`
- `Mapping[str, Any]`
- tuples such as `(stage, depends, script)`
- strings representing statuses
- strings representing scheduler types
- strings representing filesystem states
- generic `Any` contexts

Examples include `JobStage.config`, scheduler resource dictionaries, row data, environment mappings, and stage submission entries.

**Why this is a maintenance problem**

The compiler/type checker cannot establish many of the contracts.

A human must remember which keys exist, which values are legal, which values are optional, and what each string means.

The result is a large implicit API.

**Extension impact**

Adding a field or state often means updating many unrelated dictionary consumers manually.

---

### M-10 — Heavy use of `Any` weakens the internal contract between major subsystems

Examples include:

- `JobStage.run: Any`
- `write_script(..., ctx: Any)`
- `Pipeline.construct(run_context: Any)`
- `load_pipeline_source(source: Any, ...)`
- several schema loader inputs
- operation-layer contexts and helper interfaces

The AST-level inspection also shows substantial use of `Any` throughout the core modules, especially `pipeline.py`, `schema.py`, and `operations.py`.

**Why this is a maintenance problem**

The package has many object-to-object contracts, but several of the most important ones are intentionally opaque to the type system.

A developer cannot reliably discover an interface from a function signature. They must read callers, implementations, and documentation.

**Extension impact**

Changing an internal context object can silently break implementations without producing useful static feedback.

---

### M-11 — The stage API uses conventions that are stronger than the type system

`JobStage` relies on conventions such as:

- `config` containing specific keys
- `_position` being injected into configuration
- `run` having a particular runtime context
- `ctx` exposing methods such as `expand()`, `write()`, `directives()`, `preamble()`, and `epilogue()`
- `resources()` returning a dictionary with a restricted set of string keys

These contracts are mostly documented in prose rather than represented as formal interfaces.

**Why this is a maintenance problem**

A stage author must understand implementation-specific context behavior to safely extend the stage system.

The interface is therefore larger than the visible `JobStage` method declarations suggest.

**Extension impact**

Changing script-generation context behavior can break user-defined stages even when the explicit `JobStage` interface remains unchanged.

---

### M-12 — Class-level mutable configuration relies on convention rather than structural enforcement

`JobStage.settings` and `SchemaBase.fields` are mutable class-level lists/dictionaries.

The code comments explicitly tell subclasses to replace these objects wholesale rather than mutate them.

**Why this is a maintenance problem**

The correctness of the extension API depends on users following a convention.

Class-level mutable state is especially difficult to reason about because subclassing, inheritance, accidental mutation, and shared references can produce behavior that depends on class construction order or prior imports.

The implementation deliberately freezes instances, but the class-level declarations remain shared mutable objects.

**Extension impact**

User extensions can create global cross-stage or cross-schema state accidentally.

---

## C. Implicit contracts and stringly typed state

### M-13 — State is represented by scattered string constants

Examples include:

`PENDING`, `QUEUED`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`, `INVALID`, `ALIVE`, `FINISHED`, `UNKNOWN`.

These are consumed across:

- store
- scheduler
- report
- operations
- CLI
- tests

**Why this is a maintenance problem**

The state machine is distributed rather than represented as one explicit domain model.

There is no single central object defining valid transitions and semantic ownership of each state.

A human must infer the state machine by reading consumers.

**Extension impact**

Adding a new state or transition requires finding all string consumers manually.

---

### M-14 — Status semantics are duplicated and partially transformed between layers

`store.py` uses uppercase state constants while `report.py` contains a `STATUS_ORDER` with lowercase `"failed"` and `"cancelled"` entries alongside imported uppercase values.

**Why this is a maintenance problem**

The reporting layer is not simply consuming the state model; it is maintaining its own representation assumptions.

Even when this works today, it increases cognitive overhead because a developer must distinguish canonical state values from presentation values.

**Extension impact**

A new lifecycle state requires understanding both storage semantics and presentation-specific representations.

---

### M-15 — Important filesystem protocol names are embedded throughout the store

The run layout is encoded through methods such as:

- `rows_dir`
- `index_path`
- `config_path`
- `events_path`
- `lock_path`
- `stop_path`
- `done_path`
- `completions_path`
- `log_path`
- `row_dir`
- `run_dir`

The C helper independently manipulates the same names.

**Why this is a maintenance problem**

The filesystem is effectively a persistence schema, but the schema is represented by path-building methods and string literals rather than a formal persistence model.

The Python and C implementations therefore share an undocumented physical schema.

**Extension impact**

Changing one filename or directory convention requires synchronized updates across multiple layers.

---

### M-16 — Magic configuration keys are used as an internal protocol

Examples include:

- `"_position"`
- `"command"`
- `"uses"`
- `"depends"`
- `"chains_next"`
- `"extra_directives"`
- `"env"`

`"_position"` is particularly notable because it is an implementation field injected into the same dictionary as user configuration.

**Why this is a maintenance problem**

Internal metadata and user configuration occupy the same namespace.

A human reading a configuration dictionary cannot immediately tell which values came from the user's document and which were synthesized by the implementation.

**Extension impact**

Adding internal metadata creates namespace collision and validation concerns.

---

### M-17 — Tuple-based interfaces hide semantic structure

For example, scheduler pipeline entries are represented as:

`Tuple[str, str, str]`

and submission results as other tuples.

A human must remember that these positions mean stage name, dependency, and script path.

**Why this is a maintenance problem**

Positional tuples are compact but poor semantic interfaces.

Adding a field changes positional construction and consumption throughout the call graph.

**Extension impact**

The interface has low evolvability because fields cannot be added independently.

---

## D. Dynamic loading and extension architecture

### M-18 — Dynamic Python loading uses string paths and attribute names as runtime contracts

Schema and pipeline extensions are loaded through mechanisms such as:

- `"file.py:ClassName"`
- `"module.py:name"`
- stage class names inferred from stage names

Examples are in `schema.py` around `_load_python_schema()`, `_load_schema_class()`, `_import_module()`, and `_load_python_object()`, and in `pipeline.py` around `_class_name_for()` and `_build_stage()`.

**Why this is a maintenance problem**

The extension API is only partially explicit.

The system depends on:

- file location
- exact attribute spelling
- class inheritance
- module execution behavior
- class naming conventions

These are runtime contracts rather than discoverable interfaces.

**Extension impact**

Refactoring a user extension can fail at runtime rather than through static tooling.

---

### M-19 — Stage class discovery is based on name conversion conventions

`_class_name_for()` converts stage names into class names.

This creates a hidden naming convention between YAML and Python.

**Why this is a maintenance problem**

The behavior is convenient, but it makes names carry semantic meaning beyond their stated role as labels.

A stage rename can therefore change implementation resolution even when the developer intends only a presentation/configuration change.

**Extension impact**

Stage identity and implementation identity are more tightly coupled than the configuration format suggests.

---

### M-20 — Plugin loading executes arbitrary Python modules while converting broad exceptions

Examples include:

```python
except Exception as exc:
    raise SchemaError(...) from exc
```

in schema/plugin loading.

**Why this is a maintenance problem**

The loader collapses arbitrary plugin initialization failures into a generic configuration-layer exception.

This is useful at the CLI boundary but less useful for diagnosing extension code.

A human debugging a custom validator or schema may have to infer whether the failure came from jobchain, module import, class construction, or plugin initialization.

**Extension impact**

Plugin authors receive less structured failure information than the internal implementation could preserve.

---

## E. Complexity and cognitive load

### M-21 — Several functions are too large for safe local reasoning

Examples include:

- `config.load_config()` — ~85 lines
- `parse.normalize_file()` — ~69 lines
- `operations.doctor()` — ~107 lines
- `operations._prepare_fresh()` — ~80 lines
- `report.render_show()` — ~75 lines
- `cli.build_parser()` — ~143 lines
- `cli.cmd_rerun()` — ~61 lines

Large functions are not inherently incorrect, but here they occur in already large modules and often contain multiple branches and responsibilities.

**Why this is a maintenance problem**

The unit of change is too large. A developer modifying one branch must understand the surrounding control flow and side effects.

Large procedural functions also tend to accumulate special cases because the cheapest short-term extension is another conditional.

**Extension impact**

Feature growth is likely to increase branch complexity rather than compose independent behaviors.

---

### M-22 — `doctor()` is a particularly broad diagnostic function

**Location:** `jobchain/operations.py`, around line 1028.

The function is approximately 107 lines and performs many independent checks.

**Why this is a maintenance problem**

Diagnostics are naturally extensible: new consistency checks are expected over a project's lifetime.

Putting all checks into one function means every new diagnostic increases the complexity and test surface of the same function.

It also makes it difficult to identify a single reusable diagnostic rule.

**Extension impact**

The diagnostic system does not have a clearly composable check interface despite diagnostics being an obvious candidate for one.

---

### M-23 — Report rendering contains large presentation procedures rather than small render components

`report.py` includes large functions such as `render_show()` and `compute_metrics()`.

**Why this is a maintenance problem**

Status derivation, metrics, output formatting, and data extraction are interwoven more than necessary for a presentation layer.

A change to one output section can require understanding unrelated output sections.

**Extension impact**

Adding a new report format or output section increases the central rendering functions instead of adding an isolated renderer.

---

## F. Data model and persistence maintainability

### M-24 — Persistence uses ad-hoc files rather than an explicit serialized domain model

The run state is spread across files such as:

- `meta.json`
- `status`
- `status.<stage>`
- `jobid.<stage>`
- `error.<stage>`
- `timeline`
- `handoff`
- `claim`
- `gen`
- `hold`
- `manifest`
- `env`

This is a deliberate filesystem protocol, but there is no single serialization model representing a row/run state.

**Why this is a maintenance problem**

The actual schema is distributed over file names, directory structure, and the code that reads/writes them.

A human changing the state model must understand all readers and writers rather than modifying one authoritative serialization definition.

**Extension impact**

Adding state fields tends to introduce additional files or additional parsing conventions, increasing the physical protocol surface.

---

### M-25 — Serialization/deserialization logic is distributed

State construction occurs through several methods such as:

- `write_row()`
- `write_manifest()`
- `write_index()`
- `load_row()`
- `_load_run()`
- `mark()`
- `event()`
- `seed_handoff()`

**Why this is a maintenance problem**

There is no obvious single place where the complete persistent representation of a row or run is defined.

This makes it harder for a human to answer a fundamental maintenance question: "What is the authoritative representation of this state?"

**Extension impact**

A new state field can easily become write-only, read-only, or inconsistently reconstructed.

---

### M-26 — Filesystem paths are exposed as raw strings across the application

Methods such as `row_dir()`, `run_dir()`, `env_file()`, `handoff()`, and similar functions return strings.

These strings are then passed through unrelated layers.

**Why this is a maintenance problem**

The type system cannot distinguish:

- a row directory
- a run directory
- a script path
- a scheduler job ID
- a configuration path

They are all strings.

**Extension impact**

Path-related changes have a wide blast radius and rely heavily on naming discipline.

---

### M-27 — Filesystem protocol assumptions are encoded in comments and tests rather than a formal contract

The store and C helper depend on properties such as atomic `mkdir`, atomic rename behavior, file append semantics, and shared filesystem behavior.

The implementation comments explain these assumptions well, but they remain environmental contracts.

**Why this is a maintenance problem**

A future developer changing the storage mechanism has to discover these assumptions through comments and tests.

The most important concurrency guarantees are therefore not represented as explicit interfaces.

**Extension impact**

Changing the filesystem backend or concurrency mechanism would require rediscovering assumptions scattered through the implementation.

---

## G. Cross-language and duplicated source of truth

### M-28 — Version information is duplicated and currently inconsistent

`jobchain/core.py` defines:

`VERSION = "0.5-v4b"`

while `src/jobchain-node.c` defines:

`#define JC_VERSION "0.5"`

The README and CHANGELOG use `0.5-v4b`.

**Why this is a maintenance problem**

There are multiple independent version authorities.

The Python package and compute-node helper can therefore report different versions even though they are shipped as one package.

**Extension impact**

Release/version changes require remembering all locations and deciding whether every component should change.

---

### M-29 — Exit-code taxonomy is duplicated across Python and C

Python defines a taxonomy in `jobchain/core.py`, while C defines its own constants in `jobchain-node.c`.

The values overlap but are not represented by a shared source of truth.

**Why this is a maintenance problem**

Exit codes form an external API to shell scripts and users.

Duplicating them across languages creates the possibility of accidental divergence.

**Extension impact**

Adding or changing an exit code requires coordinated updates and corresponding test changes in both languages.

---

### M-30 — Protocol environment variables are duplicated across layers

Examples include:

- `JC_SCHEDULER`
- `JOBCHAIN_SCHEDULER`
- `JC_HOME`
- `JC_ROW`
- `JC_RUN`
- `JC_CHAIN`
- `JC_NEXT_ROW`
- `JC_NEXT_RUN`

These form an implicit cross-process protocol.

**Why this is a maintenance problem**

Environment variable names have effectively become an API, but their definitions are spread across Python, shell, and C.

A human changing one variable must search multiple languages to understand all producers and consumers.

**Extension impact**

Adding or renaming protocol values has cross-language compatibility implications that are not represented by a formal protocol definition.

---

## H. Error handling and diagnosability

### M-31 — Broad exception conversion hides the boundary between user/plugin errors and framework errors

Several locations catch `Exception`, including schema loading and operation-level code.

Examples:

- `schema.py` around lines 855, 978, and 1178
- `operations.py` around lines 492 and 1236
- `cli.py` around line 241

**Why this is a maintenance problem**

Broad catches make control flow easier at the outer boundary but can obscure the original category of failure.

A future developer has to inspect exception chains and surrounding context to understand what is intentionally recoverable versus what is unexpected.

**Extension impact**

New exception types can accidentally be swallowed by an existing broad handler.

---

### M-32 — The exception taxonomy is strong at the CLI boundary but weaker internally

`core.py` defines a useful hierarchy of `JobChainError` subclasses. However, many lower-level functions still operate through generic return values, dictionaries, tuples, booleans, or `Optional` results.

**Why this is a maintenance problem**

There is an inconsistency in how failure is represented.

A developer must know which subsystem communicates failure through exceptions and which communicates through return values.

**Extension impact**

Combining operations becomes more difficult because callers must handle several failure conventions.

---

### M-33 — Scheduler failure has multiple representations

Submission can produce a `Submission(success=False, output=...)`, while other scheduler failures raise `SchedulerError`.

Job-state querying can return `UNKNOWN`, while unavailable commands may be represented as `None` internally.

**Why this is a maintenance problem**

There is no single failure protocol for the scheduler subsystem.

A caller must know which operations raise, which return a status object, and which use sentinel values.

**Extension impact**

Adding scheduler operations requires deciding among several existing error conventions.

---

## I. Testing architecture as a maintainability issue

### M-34 — Tests directly depend on private implementation functions

`tests/test_operations_unit.py`, for example, imports a large set of underscore-prefixed functions from `jobchain.operations`.

`tests/test_schema_scan.py` and other tests similarly access internal implementation details.

**Why this is a maintenance problem**

The tests become coupled to implementation structure rather than stable behavior.

This makes tests resistant to healthy refactoring: moving or splitting a helper can break many tests even when external behavior remains unchanged.

**Extension impact**

The test suite itself becomes a constraint against modularization.

---

### M-35 — Several test modules are themselves subsystem-sized

Examples:

- `tests/test_operations_unit.py` — ~1,808 lines
- `tests/test_cli_unit.py` — ~901 lines
- `tests/test_integration.py` — ~800 lines
- `tests/test_store_unit.py` — ~595 lines
- `tests/test_schema_scan.py` — ~464 lines

**Why this is a maintenance problem**

Large test modules reproduce the same navigational and ownership problems as the production modules.

A developer investigating one behavior has to search a large file to determine the intended contract.

**Extension impact**

Tests become harder to discover, organize, and selectively update as features grow.

---

### M-36 — The test suite uses extensive mocking around internal implementation details

The tests make heavy use of `MagicMock`, `Mock`, `patch`, `SimpleNamespace`, and direct private-function calls.

**Why this is a maintenance problem**

Mock-heavy tests are tightly coupled to call structure.

When an internal implementation is reorganized without changing behavior, the mock expectations can become invalid.

This increases the cost of architectural refactoring.

---

### M-37 — A checked-in `MagicMock/` directory appears to contain generated/mock runtime artifacts

The archive contains:

`MagicMock/store.log_path/<numeric identifiers>`

with multiple generated-looking entries.

**Why this is a maintenance problem**

Runtime/mock artifacts in the source tree blur the boundary between source-controlled project state and generated test state.

The numeric names appear tied to object identities rather than meaningful project concepts.

**Extension impact**

Such artifacts make repository state harder to understand and can conceal accidental dependencies on generated files.

---

### M-38 — Test infrastructure is itself spread across multiple layers

Testing involves:

- Python unit tests
- Python integration tests
- example E2E tests
- shell tests
- compiled C helper tests
- sanitizer builds
- coverage
- optional Ruff
- optional mypy

`run_tests.sh` orchestrates all of them.

**Why this is a maintenance problem**

There is a large testing toolchain surface with different availability and failure behavior.

The script intentionally treats some tools as optional, meaning the exact quality gates depend on the environment.

**Extension impact**

A developer may get different validation results on different machines depending on installed tooling.

---

### M-39 — The test runner truncates test output

`run_tests.sh` pipes unittest output through `tail -25`.

**Why this is a maintenance problem**

A failed test can have important diagnostic information outside the final 25 lines.

This increases the distance between failure and actionable information.

**Extension impact**

Debugging failures in CI or unfamiliar environments becomes more difficult because the default test command intentionally hides much of the raw output.

---

## J. Repository and build maintainability

### M-40 — The repository contains a checked-in compiled binary alongside its C source

`bin/jobchain-node` is present in the archive while `src/jobchain-node.c` and the Makefile also define the build process.

**Why this is a maintenance problem**

There are now two representations of the executable:

- generated source output
- committed/shipped binary

The binary can become stale relative to the source.

The test script explicitly rebuilds it to prevent stale behavior, which is evidence that this is a real maintenance concern.

**Extension impact**

A developer can inspect source and accidentally execute a different binary than the source describes unless the build state is carefully controlled.

---

### M-41 — `install.sh` copies source, tests, examples, and build infrastructure into the installation prefix

The installation loop copies:

`jobchain src examples tests bin Makefile README.md DESIGN.md CHANGELOG.md run_tests.sh install.sh ruff.toml`

**Why this is a maintenance problem**

The boundary between a development checkout and a production installation is unclear.

A production installation therefore contains development/test/build artifacts and source files.

**Extension impact**

Future packaging changes become harder because there is no clear distinction between runtime artifacts and development artifacts.

---

### M-42 — There is no conventional Python package metadata/build configuration

The project uses a custom launcher and `PYTHONPATH` manipulation rather than a conventional package installation mechanism.

`bin/jobchain` explicitly exports the repository root into `PYTHONPATH`.

**Why this is a maintenance problem**

The package's importability depends on its filesystem layout and launcher behavior.

The module structure is therefore part of the installation contract.

**Extension impact**

Refactoring the repository layout, creating independently installable components, or integrating with standard packaging tooling becomes more complicated.

---

### M-43 — Dependency requirements are not represented as a single machine-readable package contract

The code conditionally imports PyYAML and the installation script checks for it, while the project also optionally uses coverage, Ruff, and mypy.

**Why this is a maintenance problem**

Dependency knowledge is distributed between source code, shell scripts, README material, and environment assumptions.

A developer must inspect several locations to determine what is required for runtime, testing, and development.

**Extension impact**

Adding a dependency can require manual synchronization between code and documentation/install behavior.

---

## K. API surface and naming

### M-44 — The public API re-exports a large collection of implementation classes

`jobchain/__init__.py` re-exports:

- schema validators
- schema classes
- pipeline classes
- stage settings
- pipeline loaders
- scan functions
- exception classes

This is convenient, but it creates a broad package-level public surface.

**Why this is a maintenance problem**

Anything re-exported from the package becomes a likely compatibility dependency for user code.

Internal module organization therefore becomes harder to change without preserving package-level imports.

**Extension impact**

Splitting large modules becomes more difficult because public import paths can become compatibility constraints.

---

### M-45 — `Bool` is exported under two different conceptual names

The pipeline Boolean setting is imported as `StageBool`, while the schema validator is exported as `Bool`.

This avoids a direct name collision but creates an awkward public API distinction.

**Why this is a maintenance problem**

The two types represent different concepts but have similar names and similar roles.

A user or developer must remember which Boolean abstraction belongs to which subsystem.

**Extension impact**

Future addition of other shared concepts risks increasing naming aliases and package-level ambiguity.

---

### M-46 — Configuration validation is spread across multiple independent loaders

Configuration, schema, pipeline, and stage settings each have their own validation logic.

There are multiple `_reject_unknown()`-style mechanisms and multiple conversion helpers.

**Why this is a maintenance problem**

The project has a recurring concept—"validate a structured configuration mapping"—but it is implemented separately in several places.

This produces different conventions for defaults, type conversion, unknown keys, and errors.

**Extension impact**

New configuration features require deciding which validation framework to use rather than extending one common mechanism.

---

## L. Documentation/specification as a maintenance surface

### M-47 — The project has multiple overlapping sources of behavioral truth

The archive contains:

- `README.md` — ~61 KB
- `DESIGN.md` — ~96 KB
- `CHANGELOG.md`
- `BUGFIXES.md`
- extensive source comments/docstrings
- example READMEs
- test cases
- shell helper documentation
- C source comments

**Why this is a maintenance problem**

The project has an unusually large amount of prose describing behavior.

Documentation is valuable, but each overlapping description is another place that can become stale when implementation changes.

The source comments frequently describe protocol invariants that are also described elsewhere.

**Extension impact**

A feature change can require updating several specifications to keep the repository internally consistent.

---

### M-48 — Comments sometimes describe architectural invariants that are not mechanically enforced

Examples include comments around:

- frozen stage instances
- scheduler semantics
- filesystem atomicity
- state lifecycle
- class declaration conventions
- cross-process protocol assumptions

**Why this is a maintenance problem**

The documentation tells a human what must remain true, but the architecture does not always encode those constraints structurally.

The system therefore relies on developer discipline to preserve important invariants.

**Extension impact**

A refactor can accidentally violate an invariant without producing an obvious type or interface failure.

---

# Cross-cutting findings

## M-49 — The system has too many "implicit APIs"

Several interfaces are real APIs even though they are not represented as formal interfaces:

- on-disk directory layout
- state file names
- environment variables
- scheduler command syntax
- exit codes
- generated script structure
- stage context methods
- stage configuration keys
- schema YAML keys
- dynamic plugin naming
- class-level declaration conventions
- private helper imports

**Why this matters**

Each implicit API is individually manageable. The problem is their accumulation.

A human extending the project must preserve all of them simultaneously.

This creates a high cognitive load that is not visible from the size of any one function.

---

## M-50 — The dependency graph is more tightly coupled than the module layout suggests

The project appears to have clean conceptual modules:

`config → schema → parse → pipeline → scheduler/store → operations → CLI`

but the actual implementation has cross-links such as:

- pipeline depending on a private schema import helper
- scheduler depending on a private store shell helper
- operations depending on private parse helpers
- report depending on store and parse internals
- tests reaching into private internals everywhere
- C independently reproducing store/scheduler semantics

**Why this matters**

The physical module structure gives the appearance of stronger separation than the actual dependency graph provides.

That can mislead a developer into assuming a refactor is local when it is not.

---

## M-51 — The package has a high "change amplification" factor

A single conceptual change can require edits across many locations.

Examples:

### Adding a scheduler

Likely touches:

- scheduler implementation
- directive generation
- scheduler availability
- submission
- state parsing
- cancellation
- C helper
- environment protocol
- tests
- examples
- documentation

### Adding a persistent row state

Likely touches:

- store path/schema logic
- row loading
- row writing
- state reporting
- operations
- C helper if node-side state participates
- tests
- documentation

### Adding a stage setting

Likely touches:

- pipeline setting declarations
- validation
- generated config
- stage implementation
- examples
- tests
- README/DESIGN documentation

**Why this matters**

The package is not merely large; many concepts have high change amplification because their contracts are distributed.

---

## M-52 — The architecture favors conventions and discipline over explicit extension boundaries

There are several good examples of intentional conventions:

- frozen stage instances
- subclass declarations
- naming-based stage lookup
- filesystem layout rules
- scheduler string values
- private helper conventions

The problem is that many of these conventions are simultaneously required.

**Why this matters**

A human maintainer must retain a large set of unwritten or semi-written rules.

As the project grows, this becomes increasingly difficult for a new contributor to learn and for existing contributors to preserve.

---

# Highest-risk findings for future extensibility

If the goal is specifically to make the package easier for another human to extend safely, the findings with the greatest architectural impact are:

1. **M-01 — `operations.py` is subsystem-sized.**
2. **M-02 — `schema.py` combines multiple independent layers.**
3. **M-03 — `cli.py` is too behavior-heavy.**
4. **M-04 — `store.py` owns too many responsibilities.**
5. **M-06 — scheduler support is conditional rather than polymorphic.**
6. **M-07 — Python/C scheduler behavior is duplicated.**
7. **M-08 — private helpers are cross-module dependencies.**
8. **M-09 — core contracts are dictionary/string based.**
9. **M-13 — lifecycle state is distributed rather than centrally modeled.**
10. **M-24/M-25 — persistence is an implicit filesystem schema.**
11. **M-28/M-29/M-30 — cross-language protocol/version information has multiple authorities.**
12. **M-34/M-35/M-36 — tests are tightly coupled to implementation structure.**
13. **M-40/M-41/M-42 — source, generated artifacts, and installation boundaries are blurred.**
14. **M-49/M-50/M-51 — the project has many implicit APIs and high change amplification.**

---

# Overall maintainability characterization

The codebase is not difficult to understand because it lacks documentation. In fact, it has unusually extensive documentation.

The deeper issue is that the implementation has accumulated a large number of explicit and implicit contracts without always giving each contract a narrow architectural owner.

The most important maintainability characteristic is therefore:

> **High conceptual coupling despite reasonably clear local code.**

Individual classes and functions are often documented and readable. The difficulty appears when a human asks a system-level question such as:

- "Where is the definition of row state?"
- "What is the complete scheduler interface?"
- "What files constitute the persistent state of a run?"
- "What must a custom stage context provide?"
- "What are all consumers of this environment variable?"
- "What must change to add a new scheduler?"
- "Which functions are safe to rename?"
- "Which dictionary keys are public contracts?"
- "Which source defines the authoritative version?"
- "What is the stable extension API versus an implementation detail?"

The answers are distributed across multiple modules, languages, tests, scripts, generated artifacts, and documentation.

That distribution is the central factor that makes the package increasingly difficult for a human to maintain and extend as feature count grows.

---

# Finding index by category

| ID | Category | Primary concern |
|---|---|---|
| M-01 | Architecture | `operations.py` is subsystem-sized |
| M-02 | Architecture | `schema.py` combines independent layers |
| M-03 | Architecture | `cli.py` contains substantial application behavior |
| M-04 | Architecture | `store.py` owns too many responsibilities |
| M-05 | Architecture | C helper is one large subsystem |
| M-06 | Extensibility | Scheduler is conditional rather than polymorphic |
| M-07 | Coupling | Scheduler protocol duplicated in Python/C |
| M-08 | Coupling | Private helpers are cross-module dependencies |
| M-09 | API design | Dictionary/string contracts dominate |
| M-10 | Type design | Extensive `Any` weakens contracts |
| M-11 | API design | Stage context contract is implicit |
| M-12 | Extension API | Mutable class declarations rely on convention |
| M-13 | State model | Lifecycle state is distributed |
| M-14 | State model | Presentation and canonical states differ |
| M-15 | Persistence | Filesystem names form an implicit schema |
| M-16 | Configuration | Internal/user keys share dictionaries |
| M-17 | API design | Tuples hide semantic fields |
| M-18 | Extensibility | Dynamic loading is string/path based |
| M-19 | Extensibility | Stage implementation depends on naming convention |
| M-20 | Errors | Plugin failures are broadly converted |
| M-21 | Complexity | Large functions reduce local reasoning |
| M-22 | Diagnostics | `doctor()` is monolithic |
| M-23 | Reporting | Rendering procedures are large |
| M-24 | Persistence | No single serialized domain model |
| M-25 | Persistence | Serialization is distributed |
| M-26 | Type design | Paths are untyped strings |
| M-27 | Concurrency | Filesystem guarantees are implicit |
| M-28 | Release engineering | Version authority is duplicated/inconsistent |
| M-29 | Release/API | Exit codes duplicated across languages |
| M-30 | Protocol | Environment-variable protocol is duplicated |
| M-31 | Errors | Broad exception handling |
| M-32 | Errors | Mixed error representation styles |
| M-33 | Scheduler API | Mixed scheduler failure protocols |
| M-34 | Testing | Tests depend on private implementation |
| M-35 | Testing | Test modules are subsystem-sized |
| M-36 | Testing | Heavy mock/internal coupling |
| M-37 | Repository | Generated `MagicMock` artifacts present |
| M-38 | Testing | Validation toolchain has multiple environment-dependent layers |
| M-39 | Testing | Default test runner truncates diagnostics |
| M-40 | Build | Compiled binary is present alongside source |
| M-41 | Packaging | Install tree mixes runtime/development files |
| M-42 | Packaging | Nonstandard import/install mechanism |
| M-43 | Dependencies | Dependency requirements are distributed |
| M-44 | API | Broad package-level re-export surface |
| M-45 | API | Similar concepts require aliasing (`Bool`/`StageBool`) |
| M-46 | Configuration | Multiple independent validation mechanisms |
| M-47 | Documentation | Many overlapping sources of behavioral truth |
| M-48 | Architecture | Important invariants depend on prose/convention |
| M-49 | Cross-cutting | Many implicit APIs |
| M-50 | Cross-cutting | Actual dependency graph is tightly coupled |
| M-51 | Cross-cutting | High change amplification |
| M-52 | Cross-cutting | Extension relies heavily on human discipline |

---

# Review conclusion

The strongest maintainability concern is architectural rather than stylistic.

The project has many signs of deliberate engineering: meaningful exceptions, comments explaining invariants, explicit validation, typed dataclasses in important areas, test coverage infrastructure, sanitizer support, and documented extension mechanisms.

However, these strengths are increasingly being used to manage a system whose responsibilities and contracts are distributed too broadly.

The package would become progressively harder for a new maintainer to safely modify because understanding a change requires reconstructing relationships across:

- large multi-purpose Python modules
- private cross-module helpers
- dictionaries and string protocols
- filesystem state
- dynamically loaded Python extensions
- shell launchers
- C implementation
- scheduler-specific behavior
- tests coupled to internals
- generated artifacts
- extensive documentation

The primary architectural risk is therefore **maintenance scalability**: the code can support additional features, but the human effort and cross-file reasoning required to add those features is likely to grow faster than the feature itself.
