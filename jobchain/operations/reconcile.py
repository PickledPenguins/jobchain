"""Reconciling recorded state against the scheduler and the filesystem.

A broken chain reports nothing on its own: the run does not fail, it quietly
runs fewer chains and eventually none. This is what finds that.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Tuple

from ..scheduler import ALIVE, FINISHED, describe_environment
from ..store import FAILED, PENDING, TERMINAL, Store
from ._util import _digest
from .lifecycle import PreparedRun, RunResult
from .submit import _submit_chains


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
