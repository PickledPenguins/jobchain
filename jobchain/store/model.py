"""Row/stage lifecycle state and the dataclasses that hold one run's history."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import Any, Dict, List, Optional


class RowStatus(str, Enum):
    """Row/stage lifecycle states.

    A ``str`` mixin, not a plain ``Enum``: every existing comparison
    (``row.status == DONE``), dict/set membership, and JSON serialization
    (``json.dumps`` renders a ``str`` subclass as its string value) keeps
    working unchanged, whether the other side is a ``RowStatus`` member or a
    bare string read back off disk. PENDING is implied by the absence of a
    run directory for the current generation rather than being written
    anywhere.

    ``__str__``/``__format__`` are overridden deliberately: a plain
    ``str, Enum`` mixin's ``str()``/f-string formatting returns
    ``"RowStatus.DONE"`` rather than ``"DONE"`` on some Python versions
    (this changed more than once between 3.8 and 3.12), which would
    silently break every ``f"status={value}"``-style call site. Overriding
    both here makes formatting version-stable without requiring
    ``enum.StrEnum`` (3.11+, newer than this project's stated py38 floor).
    """

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INVALID = "INVALID"
    CLAIMED = "CLAIMED"

    def __str__(self) -> str:
        return str(self.value)

    def __format__(self, spec: str) -> str:
        return format(str(self.value), spec)


# Back-compat module attributes: unchanged names, now enum members instead
# of bare strings, so every existing `from .store import DONE`-style import
# and `row.status == DONE`-style comparison is unaffected.
PENDING = RowStatus.PENDING
QUEUED = RowStatus.QUEUED
RUNNING = RowStatus.RUNNING
DONE = RowStatus.DONE
FAILED = RowStatus.FAILED
CANCELLED = RowStatus.CANCELLED
INVALID = RowStatus.INVALID
CLAIMED = RowStatus.CLAIMED

#: Statuses from which nothing further happens without intervention.
TERMINAL = {DONE, FAILED, CANCELLED, INVALID}
#: Statuses meaning the row still owes work to the scheduler.
ACTIVE = {CLAIMED, QUEUED, RUNNING}

DEFAULT_ROOT = ".jobchain"
NODE_BINARY_NAME = "jobchain-node"


@dataclass
class StageState:
    """Recorded state of one stage of one generation."""

    name: str
    status: str = PENDING
    jobid: Optional[str] = None
    error: Optional[str] = None
    depends: str = ""
    script: str = ""
    resources: Dict[str, Any] = dc_field(default_factory=dict)
    timeline: List[str] = dc_field(default_factory=list)


@dataclass
class RunState:
    """One generation of one row: its claim, its stages, its handoff."""

    generation: int
    claim: Optional[str] = None
    stages: List[StageState] = dc_field(default_factory=list)
    handoff: Dict[str, str] = dc_field(default_factory=dict)

    def stage(self, name: str) -> Optional[StageState]:
        for stage in self.stages:
            if stage.name == name:
                return stage
        return None


@dataclass
class RowState:
    """A row's identity, parameters, and history across generations."""

    name: str
    row_id: str
    line_num: int
    index: int
    params: Dict[str, Any]
    generation: int
    runs: List[RunState] = dc_field(default_factory=list)
    held: bool = False
    valid: bool = True
    invalid_reasons: List[str] = dc_field(default_factory=list)
    failure_id: str = ""
    work_dir: str = ""
    #: Raw field values, kept for rows that failed validation so they can be
    #: corrected later: their typed parameters do not exist.
    raw_fields: List[str] = dc_field(default_factory=list)

    @property
    def current(self) -> Optional[RunState]:
        for run in self.runs:
            if run.generation == self.generation:
                return run
        return None

    @property
    def attempts(self) -> int:
        return len(self.runs)

    @property
    def status(self) -> str:
        """Roll-up status, derived from the current generation's stages.

        A row is only DONE when every stage succeeded; it takes the first
        failure it finds otherwise, because that is the stage a person needs
        to look at.
        """
        if not self.valid:
            return f"failed.validation.{self.failure_id or 'unknown'}"
        run = self.current
        if run is None or not run.stages:
            return PENDING
        statuses = [stage.status for stage in run.stages]
        for stage in run.stages:
            if stage.status == FAILED:
                return f"failed.{stage.name}.{_code_of(stage.error)}"
            if stage.status == CANCELLED:
                return f"cancelled.{stage.name}"
        if all(status == DONE for status in statuses):
            return DONE
        if RUNNING in statuses:
            return RUNNING
        if QUEUED in statuses or CLAIMED in statuses:
            return QUEUED
        return PENDING

    @property
    def stage_reached(self) -> str:
        """The stage a person should look at: the failure, or the newest."""
        run = self.current
        if run is None or not run.stages:
            return ""
        for stage in run.stages:
            if stage.status in (FAILED, CANCELLED):
                return stage.name
        for stage in reversed(run.stages):
            if stage.status != PENDING:
                return stage.name
        return run.stages[0].name

    @property
    def jobid(self) -> Optional[str]:
        run = self.current
        if run is None:
            return None
        for stage in run.stages:
            if stage.name == self.stage_reached:
                return stage.jobid
        return None

    @property
    def is_terminal(self) -> bool:
        status = self.status
        return (status == DONE or status.startswith("failed.")
                or status.startswith("cancelled."))


def _code_of(error: Optional[str]) -> str:
    """Extract a compact failure code from a recorded error message."""
    if not error:
        return "unknown"
    for token in error.split():
        if token.isdigit():
            return token
    return "error"
