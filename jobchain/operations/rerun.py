"""Correcting and rerunning rows.

Correction never mutates in place. A row is taken out of circulation with a
hold file, rewritten, and only then given a new generation, so a claimer sees
either the old generation with the old parameters or the new generation with
the new ones.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import expand_template, template_is_generation_aware
from ..core import DataError, UsageError, get_logger
from ..parse import scan_row
from ..scheduler import verify_script
from ..schema import Schema
from ..store import ACTIVE, DONE, QUEUED, ManifestEntry, RowState
from .lifecycle import PreparedRun, _context_for
from .submit import _record_submissions


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

    checked = scan_row(row.line_num, row.index, fields, schema)
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
    entries: List[ManifestEntry] = []
    for spec in pipeline.specs:
        stage = pipeline.stage(spec.name)
        ctx = _context_for(prepared, row, spec)
        path = stage.write_script(row.params, ctx)
        reason = verify_script(path)
        if reason:
            raise DataError(f"row {name} stage {spec.name}: {reason}")
        entries.append(ManifestEntry(
            spec.name, spec.depends if spec.position > 1 else "-", path))
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

    entries: List[ManifestEntry] = []
    for position, stage in enumerate(stages):
        if stage not in manifest:
            continue
        depends, script = manifest[stage]
        entries.append(ManifestEntry(stage, "-" if position == 0 else depends, script))

    environment = {"JC_HOME": store.home, "JC_ROW": name, "JC_RUN": run_dir}
    if chain:
        environment["JC_CHAIN"] = "1"

    for stage_name, _, _ in entries:
        store.mark(run_dir, stage_name, QUEUED)
    results = scheduler.submit_pipeline(entries, environment)
    return _record_submissions(store, run_dir, results, scheduler)
