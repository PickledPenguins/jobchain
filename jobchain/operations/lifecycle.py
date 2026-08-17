"""Loading a run's configuration, and the run/prepare lifecycle.

Preparation is a single pass: normalize, validate, write row state, generate
scripts, submit. It is state-aware, so running the same command twice does
what remains rather than starting over.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import RunConfig, expand_template, render_final_config
from ..core import ConflictError, DataError, StateError, get_logger, log_startup_summary, trace
from ..parse import RowResult, ScanReport, normalize_file, scan
from ..pipeline import Pipeline, load_pipeline_source, single_job_pipeline
from ..scheduler import (
    NullScheduler,
    RowContext,
    RunContext,
    Scheduler,
    SchedulerBackend,
    verify_script,
)
from ..schema import Schema, apply_base_dir, load_schema_source
from ..store import ACTIVE, RUNNING, ManifestEntry, RowState, Store, row_name
from ._util import _digest, _write_json_file
from .submit import _submit_chains


@dataclass
class PreparedRun:
    """Everything loaded and resolved for one command against one run."""

    config: RunConfig
    schema: Schema
    pipeline: Pipeline
    store: Store
    scheduler: SchedulerBackend
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
    scheduler: SchedulerBackend = (NullScheduler(config.scheduler) if dry_run
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
    from ..config import load_config
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
        prepared.scheduler.write_facts(store.home)
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

    def render(row: RowState) -> Tuple[str, List[ManifestEntry], List[str]]:
        entries: List[ManifestEntry] = []
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
                entries.append(ManifestEntry(
                    spec.name, spec.depends if spec.position > 1 else "-", path))
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
