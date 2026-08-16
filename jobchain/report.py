"""Human-facing views of a run: status, show, metrics, and export.

Nothing here changes state. Every view is derived from the row directories, so
a report can always be produced, including for a run whose scheduler is no
longer reachable.

Two rules shape the output. **status always prints a table, show always prints
sections**, so choosing between them is mechanical rather than a judgment
call. And output is reported by directory with counts, never as a file
listing, because a stage may produce thousands of files.
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .parse import join_fields
from .schema import Schema
from .store import (
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    QUEUED,
    RUNNING,
    RowState,
    Store,
)

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Order statuses appear in summaries, from earliest lifecycle stage onward.
STATUS_ORDER = [PENDING, QUEUED, RUNNING, DONE, "failed", "cancelled", "INVALID"]


@dataclass
class RowView:
    """One row flattened for the status table."""

    name: str
    row_id: str
    line: int
    status: str
    stage: str
    generation: int
    attempts: int
    jobid: str
    elapsed: Optional[float]
    host: str
    error: str
    valid: bool


@dataclass
class Metrics:
    """Aggregate timing and outcome measures."""

    total: int = 0
    counts: Dict[str, int] = dc_field(default_factory=dict)
    completed: int = 0
    failed: int = 0
    invalid: int = 0
    per_stage: Dict[str, List[float]] = dc_field(default_factory=dict)
    stage_failures: Dict[str, int] = dc_field(default_factory=dict)
    first_event: Optional[dt.datetime] = None
    last_event: Optional[dt.datetime] = None
    live_chains: int = 0
    target_width: int = 0

    @property
    def finished(self) -> int:
        return self.completed + self.failed

    @property
    def remaining(self) -> int:
        return self.total - self.finished - self.invalid

    @property
    def failure_rate(self) -> Optional[float]:
        return (self.failed / self.finished) if self.finished else None

    @property
    def wall_elapsed(self) -> Optional[float]:
        if self.first_event and self.last_event:
            return (self.last_event - self.first_event).total_seconds()
        return None

    @property
    def throughput_per_hour(self) -> Optional[float]:
        elapsed = self.wall_elapsed
        if not elapsed or elapsed <= 0 or not self.finished:
            return None
        return self.finished / (elapsed / 3600.0)

    @property
    def eta_seconds(self) -> Optional[float]:
        """Projected time to finish, at the rate observed so far."""
        rate = self.throughput_per_hour
        if not rate or not self.remaining:
            return None
        return (self.remaining / rate) * 3600.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "counts": self.counts,
            "completed": self.completed,
            "failed": self.failed,
            "invalid": self.invalid,
            "remaining": self.remaining,
            "failure_rate": self.failure_rate,
            "wall_elapsed_s": self.wall_elapsed,
            "throughput_per_hour": self.throughput_per_hour,
            "eta_s": self.eta_seconds,
            "live_chains": self.live_chains,
            "target_width": self.target_width,
            "per_stage": {
                name: {
                    "mean_s": statistics.fmean(values) if values else None,
                    "median_s": statistics.median(values) if values else None,
                    "failures": self.stage_failures.get(name, 0),
                }
                for name, values in self.per_stage.items()
            },
        }


# ---------------------------------------------------------------------------
# Building views
# ---------------------------------------------------------------------------


def build_views(rows: Sequence[RowState]) -> List[RowView]:
    """Flatten row state into display records."""
    views: List[RowView] = []
    for row in rows:
        run = row.current
        stage_name = row.stage_reached
        stage = run.stage(stage_name) if run and stage_name else None
        views.append(RowView(
            name=row.name,
            row_id=row.row_id,
            line=row.line_num,
            status=row.status,
            stage=stage_name,
            generation=row.generation,
            attempts=row.attempts,
            jobid=(stage.jobid if stage and stage.jobid else ""),
            elapsed=_stage_elapsed(stage.timeline) if stage else None,
            host=_stage_host(stage.timeline) if stage else "",
            error=_first_line(stage.error) if stage and stage.error else "",
            valid=row.valid,
        ))
    return views


def filter_views(views: Sequence[RowView],
                 statuses: Optional[Sequence[str]] = None,
                 stage: Optional[str] = None) -> List[RowView]:
    """Narrow views by status prefix or by current stage.

    Matching is by prefix so that '--status failed' selects every failure and
    '--status failed.solve' selects only those at one stage.
    """
    result = list(views)
    if statuses:
        wanted = [s.lower() for s in statuses]
        result = [v for v in result if _matches(v.status, wanted)]
    if stage:
        result = [v for v in result if v.stage == stage]
    return result


def _matches(status: str, wanted: Sequence[str]) -> bool:
    """Whether a status is selected by any of the given words.

    A word matches either the start of the status, so 'failed.solve' narrows
    to one stage, or the summary category, so the words shown in the counts
    are the words that select rows.
    """
    lowered = status.lower()
    category = _category(status).lower()
    return any(lowered.startswith(word) or category == word for word in wanted)


def summarize(rows: Sequence[RowState]) -> Dict[str, int]:
    """Count rows by broad status category, in lifecycle order."""
    counts: Dict[str, int] = {}
    for row in rows:
        counts[_category(row.status)] = counts.get(_category(row.status), 0) + 1
    ordered = {key: counts[key] for key in STATUS_ORDER if key in counts}
    for key, value in counts.items():
        ordered.setdefault(key, value)
    return ordered


def _category(status: str) -> str:
    """Reduce a detailed status to the bucket a summary counts."""
    if status.startswith("failed.validation"):
        return "INVALID"
    if status.startswith("failed."):
        return "failed"
    if status.startswith("cancelled."):
        return "cancelled"
    return status


def compute_metrics(rows: Sequence[RowState], live_chains: int = 0,
                    target_width: int = 0) -> Metrics:
    """Derive timing and outcome measures from every row's history."""
    metrics = Metrics(total=len(rows), counts=summarize(rows),
                      live_chains=live_chains, target_width=target_width)
    for row in rows:
        category = _category(row.status)
        if category == DONE:
            metrics.completed += 1
        elif category == "INVALID":
            metrics.invalid += 1
        elif category in ("failed", "cancelled"):
            metrics.failed += 1

        for run in row.runs:
            for stage in run.stages:
                elapsed = _stage_elapsed(stage.timeline)
                if elapsed is not None and stage.status == DONE:
                    metrics.per_stage.setdefault(stage.name, []).append(elapsed)
                if stage.status == FAILED:
                    metrics.stage_failures[stage.name] = \
                        metrics.stage_failures.get(stage.name, 0) + 1
                for stamp in _timestamps(stage.timeline):
                    if metrics.first_event is None or stamp < metrics.first_event:
                        metrics.first_event = stamp
                    if metrics.last_event is None or stamp > metrics.last_event:
                        metrics.last_event = stamp
    return metrics


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def render_summary(counts: Dict[str, int], total: int, width: int = 40) -> List[str]:
    """Render the completion bar and the status counts."""
    lines: List[str] = []
    done = counts.get(DONE, 0)
    failed = counts.get("failed", 0) + counts.get("cancelled", 0)
    finished = done + failed

    if total:
        filled = int(width * finished / total)
        done_cells = int(width * done / total)
        bar = ("#" * done_cells + "!" * max(0, filled - done_cells)).ljust(width, ".")
        lines.append(f"[{bar}] {finished}/{total} ({100.0 * finished / total:.1f}%)")

    parts = [f"{name.upper() if name.islower() else name} {count}"
             for name, count in counts.items()]
    lines.append("   ".join(parts) if parts else "no rows")
    return lines


def render_warnings(rows: Sequence[RowState], live_chains: int,
                    target_width: int, stopped: bool) -> List[str]:
    """Warnings that belong above the table, never buried below it."""
    lines: List[str] = []
    invalid = sum(1 for row in rows if not row.valid)
    if invalid:
        lines.append(f"!  {invalid} row(s) failed validation and were never"
                     " submitted")
    if stopped:
        lines.append("!  this run is stopped; no further rows will be claimed")
    pending = sum(1 for row in rows if row.valid and row.current is None)
    if target_width and live_chains < target_width and pending:
        lines.append(f"!  {live_chains} chain(s) live, configured width "
                     f"{target_width}; chains may have been lost")
    return lines


def render_table(views: Sequence[RowView]) -> List[str]:
    """Render rows as an aligned table."""
    if not views:
        return ["no rows match"]

    headers = ["ROW", "ID", "LINE", "STATUS", "STAGE", "GEN", "TRY", "JOBID",
               "ELAPSED", "HOST"]
    table: List[List[str]] = [headers]
    for view in views:
        table.append([
            view.name,
            view.row_id,
            str(view.line),
            view.status,
            view.stage or "-",
            str(view.generation),
            str(view.attempts),
            view.jobid or "-",
            _format_duration(view.elapsed) if view.elapsed is not None else "-",
            view.host or "-",
        ])
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    return ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
            for row in table]


def render_metrics(metrics: Metrics) -> List[str]:
    """Render aggregate measures."""
    lines = [f"Finished        {metrics.finished} of {metrics.total}"]
    if metrics.invalid:
        lines.append(f"Invalid         {metrics.invalid}")
    if metrics.failure_rate is not None:
        lines.append(f"Failure rate    {metrics.failure_rate * 100:.1f}%")
    if metrics.per_stage:
        label = "Per stage"
        for name in metrics.per_stage:
            values = metrics.per_stage[name]
            failures = metrics.stage_failures.get(name, 0)
            mean = _format_duration(statistics.fmean(values)) if values else "-"
            median = _format_duration(statistics.median(values)) if values else "-"
            lines.append(f"{label:<15} {name:<10} mean {mean:<8} "
                         f"median {median:<8} failures {failures}")
            label = ""
    if metrics.wall_elapsed is not None:
        lines.append(f"Wall elapsed    {_format_duration(metrics.wall_elapsed)}")
    if metrics.throughput_per_hour is not None:
        lines.append(f"Throughput      {metrics.throughput_per_hour:.1f} rows/hour")
    if metrics.eta_seconds is not None:
        lines.append(f"Projected left  {_format_duration(metrics.eta_seconds)}"
                     f"   (assumes throughput holds)")
    if metrics.target_width:
        lines.append(f"Chains          {metrics.live_chains} of "
                     f"{metrics.target_width} live")
    return lines


def render_run_list(root: str, entries: Sequence[Dict[str, Any]]) -> List[str]:
    """Render one line per run, for when several exist."""
    headers = ["NAME", "ROWS", "DONE", "FAILED", "ACTIVE", "STARTED"]
    table = [headers]
    for entry in entries:
        table.append([
            entry["name"], str(entry["rows"]), str(entry["done"]),
            str(entry["failed"]), str(entry["active"]), entry.get("started", "-"),
        ])
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
             for row in table]
    return lines


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def render_show(row: RowState, store: Store, sections: Optional[Sequence[str]] = None,
                history: bool = False) -> List[str]:
    """Render everything known about one row, in sections sized to its state.

    A healthy row prints a short summary; a failed one leads with the failure.
    Sections are chosen by what is worth reading, not by a fixed template.
    """
    wanted = set(sections or [])
    run = row.current
    lines: List[str] = [
        (f"row {row.name}   {row.row_id}   line {row.line_num}   "
         f"generation {row.generation}   {row.status}")
    ]

    def include(name: str) -> bool:
        return not wanted or name in wanted

    if not row.valid and include("failure"):
        lines += ["", "VALIDATION"]
        for reason in row.invalid_reasons:
            lines.append(f"  {reason}")
        lines.append("  this row was never submitted")

    failed = _failed_stage(run)
    if failed is not None and include("failure"):
        lines += ["", "FAILURE"]
        lines.append(f"  stage       {failed.name}")
        if failed.error:
            lines.append(f"  message     {_first_line(failed.error)}")
        if failed.jobid:
            host = _stage_host(failed.timeline)
            lines.append(f"  job         {failed.jobid}"
                         + (f" on {host}" if host else ""))
        elapsed = _stage_elapsed(failed.timeline)
        if elapsed is not None:
            lines.append(f"  ran for     {_format_duration(elapsed)}")

    if include("parameters"):
        lines += ["", "PARAMETERS"]
        for key in sorted(row.params):
            lines.append(f"  {key:<16} {row.params[key]!r}")

    if run is not None and run.stages and include("stages"):
        lines += ["", "STAGES"]
        lines += _render_stage_table(run.stages)

    if run is not None and run.handoff and include("handoff"):
        lines += ["", "HANDOFF"]
        for key in sorted(run.handoff):
            lines.append(f"  {key:<16} {run.handoff[key]}")

    if include("paths"):
        lines += ["", "PATHS"]
        lines.append(f"  state       {store.row_dir(row.name)}")
        if row.work_dir:
            count, size = _directory_summary(row.work_dir)
            detail = f"   {count} files, {_format_size(size)}" if count else \
                "   (nothing written yet)"
            lines.append(f"  work        {row.work_dir}{detail}")
        for _, _, script in store.read_manifest(row.name):
            lines.append(f"  script      {script}")
        lines.append(f"  logs        {os.path.join(store.home, 'logs', row.name)}")

    if history and row.runs:
        lines += ["", "HISTORY"]
        for previous in row.runs:
            marker = " (current)" if previous.generation == row.generation else ""
            lines.append(f"  generation {previous.generation}{marker}")
            for past in previous.stages:
                elapsed = _stage_elapsed(past.timeline)
                lines.append(f"    {past.name:<12} {past.status:<10} "
                             f"{past.jobid or '-':<12} "
                             f"{_format_duration(elapsed) if elapsed else '-'}")

    return lines


def _render_stage_table(stages: Sequence[Any]) -> List[str]:
    """Render the per-stage table, including resources as requested."""
    headers = ["stage", "status", "job", "depends", "walltime", "ncpus", "mem",
               "elapsed", "host"]
    table = [headers]
    for stage in stages:
        resources = stage.resources or {}
        elapsed = _stage_elapsed(stage.timeline)
        table.append([
            stage.name,
            stage.status,
            stage.jobid or "-",
            stage.depends or "-",
            str(resources.get("walltime", "-")),
            str(resources.get("ncpus", "-")),
            str(resources.get("mem", "-")),
            _format_duration(elapsed) if elapsed is not None else "-",
            _stage_host(stage.timeline) or "-",
        ])
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    return ["  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
            for row in table]


def render_invalid(rows: Sequence[RowState]) -> List[str]:
    """Render every row that failed validation."""
    invalid = [row for row in rows if not row.valid]
    if not invalid:
        return ["every row passed validation"]
    lines = [f"{len(invalid)} row(s) failed validation and were never submitted",
             ""]
    headers = ["LINE", "ID", "REASON"]
    table = [headers]
    for row in invalid:
        table.append([str(row.line_num), row.row_id,
                      row.invalid_reasons[0] if row.invalid_reasons else "unknown"])
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip()
              for r in table]
    return lines


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

STATE_COLUMNS = ["status", "stage", "generation", "attempts", "elapsed_s",
                 "work_dir", "error"]


def export_rows(schema: Schema, rows: Sequence[RowState]) -> List[str]:
    """Merge parameters and state into one delimited view.

    Every original column is preserved in order, so the result is still valid
    input to the same schema, and the appended columns describe what happened.
    """
    lines = [join_fields(schema.field_names + STATE_COLUMNS, schema)]
    for row in rows:
        run = row.current
        stage_name = row.stage_reached
        stage = run.stage(stage_name) if run and stage_name else None
        elapsed = _stage_elapsed(stage.timeline) if stage else None
        values = [_render_value(row.params.get(name))
                  for name in schema.field_names]
        values += [
            row.status,
            stage_name,
            str(row.generation),
            str(row.attempts),
            f"{elapsed:.0f}" if elapsed is not None else "",
            row.work_dir,
            (_first_line(stage.error) if stage and stage.error
             else "; ".join(row.invalid_reasons)),
        ]
        lines.append(join_fields(values, schema))
    return lines


def views_to_dicts(views: Sequence[RowView]) -> List[Dict[str, Any]]:
    """Render views as plain data for JSON output."""
    return [
        {"row": v.name, "row_id": v.row_id, "line": v.line, "status": v.status,
         "stage": v.stage or None, "generation": v.generation,
         "attempts": v.attempts, "jobid": v.jobid or None,
         "elapsed_s": v.elapsed, "host": v.host or None,
         "error": v.error or None, "valid": v.valid}
        for v in views
    ]


# ---------------------------------------------------------------------------
# Timeline parsing and formatting
# ---------------------------------------------------------------------------


def _timestamps(timeline: Iterable[str]) -> List[dt.datetime]:
    return [stamp for stamp in (_parse_timestamp(e) for e in timeline)
            if stamp is not None]


def _parse_timestamp(entry: str) -> Optional[dt.datetime]:
    try:
        # Timeline stamps are local wall clock, written on the execution host,
        # and compared only with each other.
        return dt.datetime.strptime(entry[:19], _TIMESTAMP_FORMAT)  # noqa: DTZ007
    except ValueError:
        return None


def _stage_elapsed(timeline: Sequence[str]) -> Optional[float]:
    """Seconds from a stage starting to its terminal status."""
    start: Optional[dt.datetime] = None
    end: Optional[dt.datetime] = None
    for entry in timeline:
        stamp = _parse_timestamp(entry)
        if stamp is None:
            continue
        if "status=RUNNING" in entry and start is None:
            start = stamp
        if any(f"status={terminal}" in entry
               for terminal in (DONE, FAILED, CANCELLED)):
            end = stamp
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def _stage_host(timeline: Sequence[str]) -> str:
    for entry in timeline:
        for token in entry.split():
            if token.startswith("host="):
                value = token[len("host="):]
                if value and value != "unknown":
                    return value
    return ""


def _failed_stage(run: Any) -> Optional[Any]:
    if run is None:
        return None
    for stage in run.stages:
        if stage.status == FAILED:
            return stage
    return None


def _directory_summary(path: str, depth: int = 3) -> Tuple[int, int]:
    """Count files and bytes beneath a directory, bounded in depth.

    Bounded so a deep output tree cannot delay a message; an unreadable
    directory reports nothing rather than failing.
    """
    if not os.path.isdir(path):
        return 0, 0
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


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def _first_line(text: Optional[str]) -> str:
    if not text:
        return ""
    lines = text.strip().splitlines()
    return lines[0] if lines else ""


def _render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
