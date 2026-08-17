"""Cancelling jobs, and stopping a run from taking new work."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import List, Optional, Sequence, Tuple

from ..core import get_logger
from ..store import CANCELLED, TERMINAL, RowState
from .lifecycle import PreparedRun


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
