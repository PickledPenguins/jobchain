"""Submitting a row's whole pipeline, and recording the outcome.

Split out from the rest of operations: preparing a run decides *what* needs
scripts, rerun decides *which* stages to resubmit, doctor decides *whether*
a chain needs relaunching -- all three end up here to actually talk to the
scheduler and record job ids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple

from ..core import ConflictError, get_logger
from ..scheduler import SchedulerBackend
from ..store import CANCELLED, FAILED, QUEUED, Store

if TYPE_CHECKING:
    # Only used in type annotations below; importing for real would create a
    # cycle, since prepare.py calls into this module at runtime.
    from .lifecycle import PreparedRun, RunResult


def _submit_chains(prepared: "PreparedRun", width: int, result: "RunResult") -> None:
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


def _submit_row(prepared: "PreparedRun", name: str,
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
                        scheduler: Optional[SchedulerBackend] = None
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
