"""Acting on a run: preparing it, launching it, correcting it, repairing it.

Everything here is a command a person issues against a run, and all of it
goes through the same store, so the ordering guarantees hold no matter which
path is taken.

Preparation is a single pass: normalize, validate, write row state, generate
scripts, submit. It is state-aware, so running the same command twice does
what remains rather than starting over.

Correction never mutates in place. A row is taken out of circulation with a
hold file, rewritten, and only then given a new generation, so a claimer sees
either the old generation with the old parameters or the new generation with
the new ones.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import RunConfig, expand_template, render_final_config, template_is_generation_aware
from .core import (
    ConflictError,
    DataError,
    StateError,
    UsageError,
    get_logger,
    log_startup_summary,
    trace,
)
from .parse import RowResult, ScanReport, _scan_row, normalize_file, scan
from .pipeline import Pipeline, load_pipeline_source, single_job_pipeline
from .scheduler import (
    ALIVE,
    FINISHED,
    NullScheduler,
    RowContext,
    RunContext,
    Scheduler,
    describe_environment,
    verify_script,
)
from .schema import Schema, apply_base_dir, load_schema_source
from .store import (
    ACTIVE,
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    QUEUED,
    RUNNING,
    TERMINAL,
    RowState,
    Store,
    row_name,
)


@dataclass
class PreparedRun:
    """Everything loaded and resolved for one command against one run."""

    config: RunConfig
    schema: Schema
    pipeline: Pipeline
    store: Store
    scheduler: Scheduler
    run_context: RunContext


@dataclass
class RunResult:
    """Outcome of preparing and submitting a run."""

    store: Store
    scan_report: Optional[ScanReport] = None
    rows_created: int = 0
    rows_invalid: int = 0
    scripts_written: int = 0
    submitted: List[Tuple[str, List[Tuple[str, str]]]] = dc_field(default_factory=list)
    failures: List[Tuple[str, str]] = dc_field(default_factory=list)
    exhausted: bool = False
    phase: str = ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def prepare(config: RunConfig, root: Optional[str] = None,
            dry_run: bool = False) -> PreparedRun:
    """Load the schema, pipeline, and stage classes for a configuration.

    Stage classes are imported here, before any work, so a missing or broken
    class fails immediately rather than partway through generation.
    """
    schema = load_schema_source(config.schema_source, config.base_dir)
    apply_base_dir(schema, os.path.dirname(config.params_path) or ".")

    if config.pipeline_source is None:
        pipeline = single_job_pipeline()
    else:
        pipeline = load_pipeline_source(config.pipeline_source, config.base_dir)

    store = Store(config.home(root))
    scheduler: Scheduler = (NullScheduler(config.scheduler) if dry_run
                            else Scheduler(config.scheduler))

    run_context = RunContext(
        name=config.name, home=store.home, scheduler=scheduler,
        node_binary="" if dry_run else store.node_binary,
        work_dir_template=config.work_dir_template,
        log_dir_template=config.log_dir_template,
    )
    pipeline.construct(run_context)
    return PreparedRun(config, schema, pipeline, store, scheduler, run_context)


def open_run(store: Store) -> PreparedRun:
    """Reload a run from the configuration captured when it was prepared."""
    from .config import load_config
    store.require()
    final = os.path.join(store.home, "config.final.yaml")
    if not os.path.isfile(final):
        raise StateError(
            f"run '{store.name}' has no captured configuration at {final}")
    config = load_config(final)
    prepared = prepare(config, root=os.path.dirname(store.home))
    prepared.store = store
    prepared.run_context.home = store.home
    return prepared


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run(config: RunConfig, root: Optional[str] = None, check_only: bool = False,
        no_submit: bool = False, submit_only: bool = False,
        regenerate: bool = False, resume: bool = False, force: bool = False,
        dry_run: bool = False, progress: Any = None) -> RunResult:
    """Prepare and submit a run, doing whatever remains to be done."""
    logger = get_logger()
    prepared = prepare(config, root=root, dry_run=dry_run)
    store = prepared.store

    if check_only:
        report = _validate_only(prepared)
        return RunResult(store=store, scan_report=report, phase="check",
                         rows_invalid=len(report.invalid_rows),
                         rows_created=len(report.valid_rows))

    if store.exists() and not force:
        return _continue_existing(prepared, submit_only=submit_only,
                                  regenerate=regenerate, resume=resume,
                                  no_submit=no_submit, progress=progress)

    if store.exists() and force:
        logger.warning("discarding the existing run at %s", store.home)
        store.destroy()

    result = _prepare_fresh(prepared, progress=progress)
    if no_submit:
        result.phase = "prepared"
        return result

    prepared.scheduler.require_available()
    _submit_chains(prepared, config.width, result)
    result.phase = "submitted"
    return result


def _validate_only(prepared: PreparedRun) -> ScanReport:
    """Normalize and validate without writing anything."""
    config = prepared.config
    log_startup_summary("validating", {
        "params": config.params_path,
        "schema": prepared.schema.name,
        "fields": len(prepared.schema.fields),
        "stages": len(prepared.pipeline.specs),
    })
    normalized = normalize_file(config.params_path, prepared.schema)
    return scan(normalized, prepared.schema, config.params_path)


def _continue_existing(prepared: PreparedRun, submit_only: bool,
                       regenerate: bool, resume: bool, no_submit: bool,
                       progress: Any) -> RunResult:
    """Decide what remains for a run that already has state."""
    store = prepared.store
    rows = store.load_rows()
    claimed = [row for row in rows if row.current is not None]
    active = [row for row in rows if row.status in ACTIVE or row.status == RUNNING]

    if resume:
        store.resume()
        get_logger().info("stop marker cleared for run '%s'", store.name)

    if regenerate:
        result = RunResult(store=store)
        _generate_scripts(prepared, [r for r in rows if r.valid], result,
                          progress=progress)
        if no_submit:
            result.phase = "regenerated"
            return result

    if not claimed or submit_only or regenerate or resume:
        if no_submit:
            return RunResult(store=store, phase="prepared")
        _check_inputs_unchanged(prepared)
        prepared.scheduler.require_available()
        result = RunResult(store=store)
        _submit_chains(prepared, prepared.config.width, result)
        result.phase = "submitted"
        return result

    raise ConflictError(
        f"run '{store.name}' has already been started: {len(claimed)} row(s) "
        f"claimed, {len(active)} active"
    )


def _check_inputs_unchanged(prepared: PreparedRun) -> None:
    """Refuse to submit scripts that no longer match their inputs."""
    recorded = prepared.store.load_config().get("params_digest")
    if not recorded:
        return
    current = _digest(prepared.config.params_path)
    if current != recorded:
        raise ConflictError(
            f"{prepared.config.params_path} has changed since this run was "
            f"prepared; the generated scripts no longer match the file"
        )


def _prepare_fresh(prepared: PreparedRun, progress: Any = None) -> RunResult:
    """Normalize, validate, create row state, and generate scripts."""
    logger = get_logger()
    config, schema, pipeline, store = (prepared.config, prepared.schema,
                                       prepared.pipeline, prepared.store)

    log_startup_summary(f"preparing run '{config.name}'", {
        "params": config.params_path,
        "schema": schema.name,
        "pipeline": pipeline.name,
        "stages": len(pipeline.specs),
        "chaining stage": pipeline.chaining_stage,
        "scheduler": config.scheduler,
        "width": config.width,
        "workers": config.effective_workers,
        "strict": config.strict,
        "home": store.home,
    })

    store.acquire_lock()
    try:
        result = RunResult(store=store)

        normalized = normalize_file(config.params_path, schema)
        logger.info("%d row(s) read, %d normalized, %d blank and %d comment "
                    "line(s) skipped", len(normalized.rows),
                    normalized.changed_count, normalized.skipped_blank,
                    normalized.skipped_comment)

        report = scan(normalized, schema, config.params_path)
        result.scan_report = report
        for row in report.invalid_rows:
            logger.warning("row %d invalid: %s", row.line_num,
                           "; ".join(row.reasons()))
        logger.info("%d of %d row(s) valid", len(report.valid_rows),
                    len(report.rows))

        if not report.ok and config.strict:
            raise DataError(
                f"{len(report.invalid_rows)} of {len(report.rows)} row(s) "
                f"failed validation and strict mode is enabled; nothing was "
                f"created"
            )

        store.create({
            "name": config.name,
            "params": config.params_path,
            "params_digest": _digest(config.params_path),
            "scheduler": config.scheduler,
            "width": config.width,
            "max_attempts": config.max_attempts,
            "stages": pipeline.stage_names,
            "chaining_stage": pipeline.chaining_stage,
            "work_dir_template": config.work_dir_template,
            "on_complete": config.on_complete,
            "unique_fields": schema.unique_fields,
        })
        store.write_text_file("config.original.yaml", config.source_text)
        store.write_text_file(
            "config.final.yaml",
            render_final_config(config, _schema_document(prepared),
                                _pipeline_document(prepared)))
        _write_json_file(os.path.join(store.home, "scan_report.json"),
                         report.to_dict())
        _write_json_file(os.path.join(store.home, "params.normalized.json"),
                         {"rows": len(normalized.rows)})

        rows = _create_row_state(prepared, report)
        result.rows_created = sum(1 for r in rows if r.valid)
        result.rows_invalid = sum(1 for r in rows if not r.valid)

        _generate_scripts(prepared, [r for r in rows if r.valid], result,
                          progress=progress)

        store.event(f"prepared {result.rows_created} row(s), "
                    f"{result.scripts_written} script(s)")
    finally:
        store.release_lock()

    return result


def _schema_document(prepared: PreparedRun) -> Any:
    """The schema as data, for the captured configuration.

    A schema given as a path is recorded as an absolute path, because the
    captured configuration lives in the run directory and a relative path
    would resolve against the wrong place. An inline schema's own
    `validator_class`, if relative, needs the same treatment: it is
    resolved against the original configuration's directory today, but a
    reload later reads the captured document from the run directory, so an
    un-absolutized relative path would look for the module in the wrong
    place at that point.
    """
    source = prepared.config.schema_source
    if isinstance(source, dict):
        document = dict(source)
        validator = document.get("validator_class")
        if validator and not os.path.isabs(str(validator)):
            document["validator_class"] = os.path.join(
                prepared.config.base_dir, str(validator))
        return document
    return os.path.join(prepared.config.base_dir, str(source)) \
        if not os.path.isabs(str(source)) else str(source)


def _pipeline_document(prepared: PreparedRun) -> Any:
    """The pipeline as data, with module and script paths absolutized.

    Relative paths in the original configuration are relative to that file.
    The captured configuration sits in the run directory, so those paths are
    resolved now; otherwise reopening the run would look for stage classes
    beside the state rather than beside the configuration.
    """
    source = prepared.config.pipeline_source
    if source is None:
        return None
    if not isinstance(source, dict):
        path = str(source)
        return path if os.path.isabs(path) \
            else os.path.join(prepared.config.base_dir, path)

    document = dict(source)
    module = document.get("stage_module")
    if module and not os.path.isabs(str(module)):
        document["stage_module"] = os.path.join(prepared.config.base_dir,
                                                str(module))
    return document


def _create_row_state(prepared: PreparedRun, report: ScanReport) -> List[RowState]:
    """Write state for every row, valid or not.

    Invalid rows get state so they can be corrected into a running run later.
    They get no scripts and no manifest, which is what keeps them unclaimable
    until they are fixed.
    """
    store, schema, config = prepared.store, prepared.schema, prepared.config
    names: List[str] = []
    rows: List[RowState] = []

    for result in report.rows:
        name = row_name(result.index)
        record = result.record or {}
        identifier = _identifier_for(schema, result, record, name)
        work_dir = ""
        if result.ok:
            work_dir = expand_template(
                config.work_dir_template, config.name, store.home,
                row=record, row_name=name, row_index=result.index, generation=1)
        store.write_row(
            name=name, row_id=identifier, line_num=result.line_num,
            index=result.index, params=record, generation=1,
            valid=result.ok, invalid_reasons=result.reasons(),
            failure_id="" if result.ok else result.failure_id(),
            work_dir=work_dir,
            raw_fields=[] if result.ok else result.raw_fields,
        )
        names.append(name)
        rows.append(store.load_row(name))

    store.write_index(names)
    return rows


def _context_for(prepared: PreparedRun, row: RowState, spec: Any) -> RowContext:
    """Build the context for one row and stage, and settle the script path.

    The working directory is expanded first, because a stage's output_dir
    defaults to it; the stage then chooses where its script goes, and only
    then is the script path known.
    """
    stage = prepared.pipeline.stage(spec.name)
    work_dir = prepared.run_context.work_dir(
        row.params, row_name=row.name, row_index=row.index,
        generation=row.generation)
    ctx = RowContext(
        run=prepared.run_context, row_name=row.name, row_index=row.index,
        stage=spec.name, generation=row.generation, work_dir=work_dir,
        chains_next=spec.chains_next,
    )
    directory = stage.output_dir(row.params, ctx)
    ctx.script_path = os.path.join(directory, stage.script_name(row.params))
    return ctx


def _identifier_for(schema: Schema, result: RowResult, record: Dict[str, Any],
                    name: str) -> str:
    """The identifier shown for a row, valid or not.

    A row that failed validation has no typed values, so its raw text is used
    instead: a row that cannot be named cannot be corrected.
    """
    if not schema.id_field:
        return name
    if record:
        return str(record.get(schema.id_field, name))
    try:
        position = schema.field_names.index(schema.id_field)
    except ValueError:
        return name
    if position < len(result.raw_fields):
        candidate = result.raw_fields[position].strip()
        if candidate:
            return candidate
    return name


def _generate_scripts(prepared: PreparedRun, rows: Sequence[RowState],
                      result: RunResult, progress: Any = None) -> None:
    """Write one script per stage per valid row, across a thread pool.

    The work is embarrassingly parallel: no script depends on another. Stage
    instances are frozen, so there is no shared mutable state to race on.
    Every failure is collected rather than aborting on the first, because a
    class that breaks for one row usually breaks for many and reporting them
    one at a time wastes the user's time.
    """
    logger = get_logger()
    pipeline, store = prepared.pipeline, prepared.store
    total = len(rows) * len(pipeline.specs)
    if total == 0:
        return

    logger.info("generating %d script(s) across %d worker(s)",
                total, prepared.config.effective_workers)
    if progress is not None:
        progress.start(total)

    failures: List[str] = []
    written = 0

    def render(row: RowState) -> Tuple[str, List[Tuple[str, str, str]], List[str]]:
        entries: List[Tuple[str, str, str]] = []
        problems: List[str] = []
        for spec in pipeline.specs:
            stage = pipeline.stage(spec.name)
            try:
                ctx = _context_for(prepared, row, spec)
                path = stage.write_script(row.params, ctx)
                reason = verify_script(path)
                if reason:
                    problems.append(f"row {row.name} stage {spec.name}: {reason}")
                    continue
                entries.append((spec.name, spec.depends if spec.position > 1 else "-",
                                path))
                trace("wrote %s (row %s, stage %s)", path, row.name, spec.name)
            except Exception as exc:
                problems.append(f"row {row.name} stage {spec.name}: {exc}")
        return row.name, entries, problems

    with ThreadPoolExecutor(max_workers=prepared.config.effective_workers) as pool:
        futures = [pool.submit(render, row) for row in rows]
        for future in as_completed(futures):
            name, entries, problems = future.result()
            failures.extend(problems)
            if entries and not problems:
                store.write_manifest(name, entries)
                written += len(entries)
            if progress is not None:
                progress.advance(len(entries) + len(problems))

    if progress is not None:
        progress.finish()

    if failures:
        shown = "\n  ".join(failures[:20])
        more = f"\n  ... and {len(failures) - 20} more" if len(failures) > 20 else ""
        raise DataError(f"{len(failures)} script(s) could not be generated:\n  "
                        f"{shown}{more}")

    result.scripts_written = written


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def _submit_chains(prepared: PreparedRun, width: int, result: RunResult) -> None:
    """Claim and submit rows until width chains are running or rows run out."""
    logger = get_logger()
    store = prepared.store

    if store.stopped:
        raise ConflictError(
            f"run '{store.name}' is stopped; no rows will be claimed until it "
            f"is resumed"
        )

    # A ceiling on pipelines submitted but not finished. Unset by default, so
    # nothing is throttled unless asked; with it, a fast first stage cannot
    # queue the whole file while later stages are still running.
    ceiling = int(prepared.config.max_in_flight or 0)
    if ceiling:
        in_flight = sum(1 for row in store.load_rows()
                        if row.current is not None and not row.is_terminal)
        room = max(0, ceiling - in_flight)
        if room < width:
            logger.info("%d pipeline(s) already in flight against a ceiling of "
                        "%d; launching %d instead of %d",
                        in_flight, ceiling, room, width)
            width = room

    for index in range(width):
        claimed = store.claim()
        if claimed is None:
            result.exhausted = True
            logger.info("no further rows available after launching %d chain(s)",
                        len(result.submitted))
            break
        name, run_dir = claimed
        jobs, failure = _submit_row(prepared, name, run_dir)
        if failure:
            result.failures.append((name, failure))
            logger.error("chain %d: row %s failed to submit: %s",
                         index + 1, name, failure)
        else:
            result.submitted.append((name, jobs))
            logger.info("chain %d: row %s submitted (%s)", index + 1, name,
                        ", ".join(f"{s} {j}" for s, j in jobs))


def _submit_row(prepared: PreparedRun, name: str,
                run_dir: str) -> Tuple[List[Tuple[str, str]], str]:
    """Submit one row's whole pipeline, threading dependency job ids.

    If a stage is rejected, everything already submitted for the row is
    cancelled: leaving a partial pipeline queued would strand its successors
    behind a dependency that can never be satisfied.
    """
    store, scheduler = prepared.store, prepared.scheduler
    entries = store.read_manifest(name)
    if not entries:
        reason = "no manifest: the row has no generated scripts"
        return [], reason

    row = store.load_row(name)
    environment = {"JC_HOME": store.home, "JC_ROW": name, "JC_RUN": run_dir,
                   "JC_CHAIN": "1"}

    for spec in prepared.pipeline.specs:
        stage = prepared.pipeline.stage(spec.name)
        store.write_resources(run_dir, spec.name,
                              stage.effective_resources(row.params))

    for stage_name, _, _ in entries:
        store.mark(run_dir, stage_name, QUEUED)

    results = scheduler.submit_pipeline(entries, environment)
    return _record_submissions(store, run_dir, results, scheduler)


def _record_submissions(store: Store, run_dir: str,
                        results: Sequence[Tuple[str, Any]],
                        scheduler: Optional[Scheduler] = None
                        ) -> Tuple[List[Tuple[str, str]], str]:
    """Record job ids, rolling back the row if a stage was rejected."""
    jobs: List[Tuple[str, str]] = []
    for stage_name, submission in results:
        if not submission.success:
            # A pipeline missing a middle stage would strand its successors
            # behind a dependency that can never be satisfied, so what was
            # already submitted is cancelled.
            for done_stage, done_job in jobs:
                if scheduler is not None:
                    scheduler.cancel(done_job)
                store.mark(run_dir, done_stage, CANCELLED,
                           error="cancelled: a later stage was rejected")
            store.mark(run_dir, stage_name, FAILED,
                       error=submission.output or "submission rejected")
            return jobs, submission.output or "submission rejected"
        jobs.append((stage_name, submission.job_id or "unknown"))
        store.mark(run_dir, stage_name, jobid=submission.job_id)
    return jobs, ""


# ---------------------------------------------------------------------------
# rerun
# ---------------------------------------------------------------------------


@dataclass
class RerunPlan:
    """What a rerun would do, before it does it."""

    rows: List[RowState] = dc_field(default_factory=list)
    stages: List[str] = dc_field(default_factory=list)
    changes: Dict[str, Dict[str, Tuple[Any, Any]]] = dc_field(default_factory=dict)
    new_generation: bool = True
    needs_confirmation: List[Tuple[RowState, List[Tuple[str, int, int]]]] = \
        dc_field(default_factory=list)
    skipped: List[Tuple[str, str]] = dc_field(default_factory=list)


@dataclass
class RerunResult:
    """Outcome of a rerun."""

    rows: List[str] = dc_field(default_factory=list)
    submitted: List[Tuple[str, List[Tuple[str, str]]]] = dc_field(default_factory=list)
    failures: List[Tuple[str, str]] = dc_field(default_factory=list)
    skipped: List[Tuple[str, str]] = dc_field(default_factory=list)
    regenerated: int = 0


def plan_rerun(prepared: PreparedRun, rows: Sequence[RowState],
               assignments: Optional[Dict[str, str]] = None,
               stages: Optional[Sequence[str]] = None,
               from_stage: Optional[str] = None,
               force: bool = False) -> RerunPlan:
    """Work out what a rerun would do, including what it might overwrite."""
    pipeline = prepared.pipeline
    plan = RerunPlan()

    if from_stage:
        names = pipeline.stage_names
        if from_stage not in names:
            raise UsageError(
                f"no stage named '{from_stage}'; stages are {names}")
        plan.stages = names[names.index(from_stage):]
    elif stages:
        for stage in stages:
            if stage not in pipeline.stage_names:
                raise UsageError(
                    f"no stage named '{stage}'; stages are {pipeline.stage_names}")
        plan.stages = [s for s in pipeline.stage_names if s in stages]
    else:
        plan.stages = list(pipeline.stage_names)

    # A partial rerun keeps the current generation, so earlier stages' results
    # and handoff values remain in place.
    plan.new_generation = not (stages or from_stage)

    limit = int(prepared.store.load_config().get("max_attempts") or 0)
    generation_aware = template_is_generation_aware(
        prepared.config.work_dir_template)

    for row in rows:
        if row.status in ACTIVE and not force:
            plan.skipped.append(
                (row.name, f"still {row.status}; cancel it or use --force"))
            continue
        if limit and row.attempts >= limit and not force:
            plan.skipped.append(
                (row.name,
                 f"already attempted {row.attempts} time(s), limit is {limit}"))
            continue
        if assignments:
            plan.changes[row.name] = _describe_changes(
                prepared.schema, row, assignments)
        if row.status == DONE and not generation_aware:
            existing = _existing_output(row)
            if existing:
                plan.needs_confirmation.append((row, existing))
        plan.rows.append(row)

    return plan


def _describe_changes(schema: Schema, row: RowState,
                      assignments: Dict[str, str]) -> Dict[str, Tuple[Any, Any]]:
    unknown = set(assignments) - set(schema.field_names)
    if unknown:
        raise UsageError(
            f"unknown column(s) {sorted(unknown)}; the schema declares "
            f"{schema.field_names}")
    return {key: (row.params.get(key), value) for key, value in assignments.items()}


def _existing_output(row: RowState) -> List[Tuple[str, int, int]]:
    """Top-level output directories that already hold files.

    Directories, never file listings: a stage may produce thousands of files,
    and the useful question is which trees are at risk, not which files.
    """
    if not row.work_dir or not os.path.isdir(row.work_dir):
        return []
    found: List[Tuple[str, int, int]] = []
    try:
        entries = sorted(os.listdir(row.work_dir))
    except OSError:
        return []
    for entry in entries:
        path = os.path.join(row.work_dir, entry)
        if os.path.isdir(path):
            count, size = _directory_size(path)
            if count:
                found.append((path, count, size))
    if not found:
        count, size = _directory_size(row.work_dir, depth=1)
        if count:
            found.append((row.work_dir, count, size))
    return found


def _directory_size(path: str, depth: int = 3) -> Tuple[int, int]:
    """Count files and bytes beneath a directory, bounded in depth.

    Bounded so that a deep output tree cannot delay a message; a directory
    that cannot be read reports nothing rather than failing.
    """
    count = 0
    size = 0
    root_depth = path.rstrip(os.sep).count(os.sep)
    try:
        for current, directories, files in os.walk(path):
            if current.count(os.sep) - root_depth >= depth:
                directories[:] = []
            for name in files:
                count += 1
                try:
                    size += os.path.getsize(os.path.join(current, name))
                except OSError:
                    pass
    except OSError:
        return 0, 0
    return count, size


def execute_rerun(prepared: PreparedRun, plan: RerunPlan,
                  assignments: Optional[Dict[str, str]] = None,
                  regenerate: bool = False, chain: bool = False,
                  fresh_handoff: bool = False,
                  dry_run: bool = False) -> RerunResult:
    """Apply a rerun plan: correct values, regenerate, re-queue, submit."""
    logger = get_logger()
    store = prepared.store
    result = RerunResult(skipped=list(plan.skipped))

    for row in plan.rows:
        if dry_run:
            result.rows.append(row.name)
            continue

        if assignments:
            _apply_changes(prepared, row, assignments)

        # The generation is raised before scripts are regenerated, because a
        # script bakes in the run directory it reports to. Regenerating first
        # would leave the new jobs writing status into the previous
        # generation's directory.
        if plan.new_generation:
            previous = store.load_row(row.name).current
            if fresh_handoff:
                store.clear_handoff_seed(row.name)
            elif previous and previous.handoff:
                # Carry values forward so a later partial rerun still sees
                # what earlier stages produced.
                store.seed_handoff(row.name, previous.handoff)
            generation = store.bump_generation(row.name)
            logger.info("row %s re-queued at generation %d", row.name, generation)

        if assignments or regenerate:
            result.regenerated += _regenerate_row(prepared, row.name)
        result.rows.append(row.name)

    if dry_run:
        return result

    store.event(f"rerun: {len(result.rows)} row(s)")

    # A partial rerun submits directly; a full rerun leaves the row claimable
    # so the next free chain picks it up, unless asked to launch now.
    for row in plan.rows:
        if plan.new_generation and not chain:
            continue
        jobs, failure = _submit_selected(prepared, row.name, plan.stages, chain)
        if failure:
            result.failures.append((row.name, failure))
        else:
            result.submitted.append((row.name, jobs))
    return result


def _apply_changes(prepared: PreparedRun, row: RowState,
                   assignments: Dict[str, str]) -> None:
    """Validate new values, then rewrite the row behind a hold."""
    schema, store = prepared.schema, prepared.store
    fields = _raw_fields(schema, row)
    for name, value in assignments.items():
        fields[schema.field_names.index(name)] = value

    checked = _scan_row(row.line_num, row.index, fields, schema)
    if not checked.ok:
        raise DataError(
            f"the revised values for row {row.name} do not validate: "
            + "; ".join(checked.reasons()))

    record = checked.record or {}
    identifier = (str(record.get(schema.id_field, row.row_id))
                  if schema.id_field else row.row_id)
    work_dir = expand_template(
        prepared.config.work_dir_template, prepared.config.name, store.home,
        row=record, row_name=row.name, row_index=row.index,
        generation=row.generation)

    store.hold(row.name)
    try:
        store.write_row(name=row.name, row_id=identifier, line_num=row.line_num,
                        index=row.index, params=record, generation=row.generation,
                        valid=True, work_dir=work_dir)
    finally:
        # The hold must come off even if a write failed, or the row would be
        # excluded from claiming for the rest of the run.
        store.release(row.name)

    described = ", ".join(f"{k}: {row.params.get(k)!r} -> {v!r}"
                          for k, v in assignments.items())
    get_logger().info("row %s revised: %s", row.name, described)


def _raw_fields(schema: Schema, row: RowState) -> List[str]:
    """Recover a row's values as strings, in schema field order.

    A row that failed validation has no typed parameters, so its recorded raw
    text is used; otherwise a correction would discard every other column.
    """
    fields: List[str] = []
    if not row.params and row.raw_fields:
        fields = list(row.raw_fields)
        while len(fields) < len(schema.field_names):
            fields.append("")
        return fields[:len(schema.field_names)]

    for name in schema.field_names:
        value = row.params.get(name)
        if value is None:
            fields.append("")
        elif isinstance(value, bool):
            fields.append("true" if value else "false")
        else:
            fields.append(str(value))
    return fields


def _regenerate_row(prepared: PreparedRun, name: str) -> int:
    """Rewrite every stage script for one row."""
    store, pipeline = prepared.store, prepared.pipeline
    row = store.load_row(name)
    entries: List[Tuple[str, str, str]] = []
    for spec in pipeline.specs:
        stage = pipeline.stage(spec.name)
        ctx = _context_for(prepared, row, spec)
        path = stage.write_script(row.params, ctx)
        reason = verify_script(path)
        if reason:
            raise DataError(f"row {name} stage {spec.name}: {reason}")
        entries.append((spec.name, spec.depends if spec.position > 1 else "-", path))
    store.write_manifest(name, entries)
    return len(entries)


def _submit_selected(prepared: PreparedRun, name: str, stages: Sequence[str],
                     chain: bool) -> Tuple[List[Tuple[str, str]], str]:
    """Submit a subset of a row's stages, in order."""
    store, scheduler = prepared.store, prepared.scheduler
    row = store.load_row(name)
    manifest = {stage: (depends, script)
                for stage, depends, script in store.read_manifest(name)}
    if not manifest:
        return [], "no manifest: the row has no generated scripts"

    run_dir = store.run_dir(name, row.generation)
    os.makedirs(run_dir, exist_ok=True)

    entries = []
    for position, stage in enumerate(stages):
        if stage not in manifest:
            continue
        depends, script = manifest[stage]
        entries.append((stage, "-" if position == 0 else depends, script))

    environment = {"JC_HOME": store.home, "JC_ROW": name, "JC_RUN": run_dir}
    if chain:
        environment["JC_CHAIN"] = "1"

    for stage_name, _, _ in entries:
        store.mark(run_dir, stage_name, QUEUED)
    results = scheduler.submit_pipeline(entries, environment)
    return _record_submissions(store, run_dir, results, scheduler)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@dataclass
class CancelResult:
    """Outcome of cancelling."""

    cancelled: List[Tuple[str, List[str]]] = dc_field(default_factory=list)
    stopped: bool = False
    skipped: List[Tuple[str, str]] = dc_field(default_factory=list)


def cancel(prepared: PreparedRun, rows: Sequence[RowState],
           stage: Optional[str] = None, stop: bool = False,
           dry_run: bool = False) -> CancelResult:
    """Cancel jobs, and optionally stop the chain from taking new work.

    Cancelling every stage of a row is the default because a scheduler's own
    cascade cannot be relied on: a job removed before it ran never terminates,
    so no dependency type fires and its successors would sit unsatisfiable.
    """
    logger = get_logger()
    store, scheduler = prepared.store, prepared.scheduler
    result = CancelResult()

    if stop and not dry_run:
        store.stop("cancelled by request")
        result.stopped = True
        logger.info("run '%s' stopped: no further rows will be claimed",
                    store.name)

    for row in rows:
        run = row.current
        if run is None:
            result.skipped.append((row.name, "never claimed"))
            continue
        killed: List[str] = []
        for stage_state in run.stages:
            if stage and stage_state.name != stage:
                continue
            if not stage_state.jobid or stage_state.status in TERMINAL:
                continue
            if dry_run:
                killed.append(stage_state.jobid)
                continue
            ok, output = scheduler.cancel(stage_state.jobid)
            store.mark(store.run_dir(row.name, row.generation), stage_state.name,
                       CANCELLED, error=output or "cancelled by request")
            killed.append(stage_state.jobid)
            if not ok:
                logger.warning("row %s stage %s: %s", row.name,
                               stage_state.name, output)
        if killed:
            result.cancelled.append((row.name, killed))
            logger.info("row %s: cancelled %d job(s)", row.name, len(killed))
        else:
            result.skipped.append((row.name, "nothing active"))

    if result.cancelled and not dry_run:
        store.event(f"cancelled {len(result.cancelled)} row(s)")
    return result


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One problem discovered during reconciliation."""

    row: str
    detail: str
    repaired: bool = False


@dataclass
class DoctorResult:
    """Outcome of a reconciliation pass."""

    run: str = ""
    findings: List[Finding] = dc_field(default_factory=list)
    live_chains: int = 0
    target_width: int = 0
    total_rows: int = 0
    finished_rows: int = 0
    active_rows: int = 0
    pending_rows: int = 0
    relaunched: List[Tuple[str, List[Tuple[str, str]]]] = dc_field(default_factory=list)
    environment: Dict[str, str] = dc_field(default_factory=dict)
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return not self.findings


def doctor(prepared: PreparedRun, repair: bool = False,
           dry_run: bool = False) -> DoctorResult:
    """Compare recorded state against the scheduler and the filesystem.

    A broken chain reports nothing on its own: the run does not fail, it
    quietly runs fewer chains and eventually none. This is what finds that.
    """
    store, scheduler = prepared.store, prepared.scheduler
    config = store.load_config()
    result = DoctorResult(run=store.name,
                          target_width=int(config.get("width", 1)),
                          environment=describe_environment(),
                          stopped=store.stopped)

    rows = store.load_rows()
    result.total_rows = len(rows)
    chaining_stage = config.get("chaining_stage", "")

    for row in rows:
        if not row.valid:
            continue
        run = row.current
        if run is None:
            result.pending_rows += 1
            continue
        if row.is_terminal:
            result.finished_rows += 1
        else:
            result.active_rows += 1

        alive_here = False
        for stage_state in run.stages:
            if stage_state.status in TERMINAL or stage_state.status == PENDING:
                continue
            if not stage_state.jobid:
                result.findings.append(Finding(
                    row.name,
                    f"stage {stage_state.name} is {stage_state.status} with no "
                    f"job id; the submitting process did not finish"))
                if repair and not dry_run:
                    store.mark(store.run_dir(row.name, row.generation),
                               stage_state.name, FAILED,
                               error="claimed but never submitted")
                    result.findings[-1].repaired = True
                continue

            state = scheduler.job_state(stage_state.jobid)
            if state == ALIVE:
                alive_here = True
            elif state == FINISHED:
                result.findings.append(Finding(
                    row.name,
                    f"stage {stage_state.name} is recorded {stage_state.status} "
                    f"but job {stage_state.jobid} is no longer known to the "
                    f"scheduler"))
                if repair and not dry_run:
                    store.mark(store.run_dir(row.name, row.generation),
                               stage_state.name, FAILED,
                               error=f"job {stage_state.jobid} vanished")
                    result.findings[-1].repaired = True

        if alive_here:
            result.live_chains += 1
        elif chaining_stage and not row.is_terminal:
            chaining = run.stage(chaining_stage)
            if chaining is not None and chaining.status in TERMINAL:
                continue
            result.findings.append(Finding(
                row.name,
                f"no live job, and the chaining stage '{chaining_stage}' has "
                f"not run; the chain ended here"))

        for stage_state in run.stages:
            if stage_state.script and not os.path.isfile(stage_state.script):
                result.findings.append(Finding(
                    row.name,
                    f"stage {stage_state.name} script no longer exists: "
                    f"{stage_state.script}"))

    _check_params_digest(store, config, result)

    invalid = [row for row in rows if not row.valid]
    if invalid:
        result.findings.append(Finding(
            "-", f"{len(invalid)} row(s) failed validation and were never "
                 f"submitted"))

    if result.stopped:
        result.findings.append(Finding(
            "-", "the run is stopped; no rows will be claimed until it resumes"))

    started = any(row.current is not None for row in rows)
    shortfall = result.target_width - result.live_chains
    # A run that was prepared but never started is not "short of chains": it
    # has none by design, and saying so would be noise.
    if started and shortfall > 0 and result.pending_rows > 0:
        result.findings.append(Finding(
            "-", f"{shortfall} chain(s) short of the configured width "
                 f"{result.target_width}"))
        if repair and not dry_run and not result.stopped:
            launched = RunResult(store=store)
            prepared.scheduler.require_available()
            _submit_chains(prepared, shortfall, launched)
            result.relaunched = launched.submitted
            result.findings[-1].repaired = bool(launched.submitted)

    return result


def _check_params_digest(store: Store, config: Dict[str, Any],
                         result: DoctorResult) -> None:
    """Detect the parameter file being edited outside this tool."""
    params = config.get("params")
    recorded = config.get("params_digest")
    if not params or not recorded:
        return
    if not os.path.isfile(params):
        result.findings.append(Finding("-", f"the parameter file {params} no "
                                            f"longer exists"))
        return
    if _digest(params) != recorded:
        result.findings.append(Finding(
            "-", f"{params} has changed since this run was prepared; the "
                 f"running rows reflect the original file"))


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def check_completion(store: Store, on_complete: str = "") -> Optional[Dict[str, Any]]:
    """Write or remove the done marker, and run the completion hook.

    The marker is present only while nothing is outstanding, so a rerun
    removes it immediately. A counter distinguishes the first completion from
    one reached after corrections.
    """
    rows = store.load_rows()
    if not rows:
        return None
    outstanding = [row for row in rows if row.valid and not row.is_terminal]

    if outstanding:
        if os.path.exists(store.done_path):
            os.unlink(store.done_path)
        return None

    if os.path.exists(store.done_path):
        return None  # already recorded; nothing has changed

    previous = _read_completions(store)
    payload: Dict[str, Any] = {
        "run": store.name,
        "completion": len(previous) + 1,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "first_completed_at": previous[0] if previous
        else time.strftime("%Y-%m-%dT%H:%M:%S"),
        "work_dirs": sorted({r.work_dir for r in rows if r.work_dir})[:20],
    }
    counts: Dict[str, int] = {
        "total": len(rows),
        "done": sum(1 for r in rows if r.status == DONE),
        "failed": sum(1 for r in rows if r.status.startswith("failed.") and r.valid),
        "invalid": sum(1 for r in rows if not r.valid),
    }
    payload["rows"] = counts
    _write_json_file(store.done_path, payload)
    with open(store.completions_path, "a", encoding="utf-8") as handle:
        handle.write(f"{payload['completed_at']} completion="
                     f"{payload['completion']} done={counts['done']} "
                     f"failed={counts['failed']}\n")
    counts = payload["rows"]
    get_logger().info("run complete (completion %d): %d done, %d failed, "
                      "%d invalid", payload["completion"], counts["done"],
                      counts["failed"], counts["invalid"])

    if on_complete:
        _run_hook(store, on_complete, payload)
    return payload


def _read_completions(store: Store) -> List[str]:
    if not os.path.isfile(store.completions_path):
        return []
    with open(store.completions_path, "r", encoding="utf-8") as handle:
        return [line.split()[0] for line in handle if line.strip()]


def _run_hook(store: Store, command: str, payload: Dict[str, Any]) -> None:
    """Run the completion hook, never letting it fail the run."""
    counts: Dict[str, int] = dict(payload["rows"])  # type: ignore[arg-type]
    environment = dict(os.environ)
    environment.update({
        "JC_RUN_NAME": store.name,
        "JC_HOME": store.home,
        "JC_COMPLETION": str(payload["completion"]),
        "JC_ROWS_DONE": str(counts["done"]),
        "JC_ROWS_FAILED": str(counts["failed"]),
    })
    expanded = command.replace("{run.home}", store.home).replace(
        "{run.name}", store.name)
    try:
        completed = subprocess.run(expanded, shell=True, env=environment,
                                   capture_output=True, text=True, timeout=300)
        if completed.returncode != 0:
            get_logger().warning("on_complete exited %d: %s",
                                 completed.returncode, completed.stderr.strip())
    except Exception as exc:
        get_logger().warning("on_complete could not be run: %s", exc)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _digest(path: str) -> str:
    """Content digest of a file, used to detect external edits."""
    import hashlib
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _write_json_file(path: str, payload: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
