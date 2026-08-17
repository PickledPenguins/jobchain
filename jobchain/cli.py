"""Command line front end.

This is the only module that parses arguments or prints for a person; every
layer beneath it returns data. It is also the single place where an exception
becomes an exit code, which upholds the rule that a traceback reaching the
terminal always indicates a defect in this tool rather than bad input.

Messages state what happened and stop. Suggested follow-up commands are
deliberately absent: hint text spread across dozens of messages drifts out of
step with the commands it names. Commands are documented once, in the help.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from . import operations, report
from .config import RunConfig, load_config
from .core import (
    EXIT_INTERNAL,
    EXIT_NAMES,
    EXIT_OK,
    EXIT_USAGE,
    VERSION,
    ConflictError,
    JobChainError,
    StateError,
    UsageError,
    configure_logging,
    get_logger,
)
from .parse import format_report
from .store import ACTIVE, DONE, Store

PROGRAM = "jobchain"
WATCH_INTERVAL_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the command line grammar."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Run scheduler job pipelines from a delimited parameter file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run", metavar="NAME", dest="run_selector",
                        help="which run to act on; needed only when several exist")
    common.add_argument("-v", "--verbose", action="count", default=0,
                        help="console detail: progress, then full trace")
    common.add_argument("--log-level", metavar="LEVEL",
                        help="console level: error, warning, info, debug, trace")
    common.add_argument("--file-log-level", metavar="LEVEL",
                        help="level recorded in the run's log file")
    common.add_argument("--json", action="store_true", dest="as_json",
                        help="machine-readable output")
    common.add_argument("--dry-run", action="store_true",
                        help="report what would happen; change nothing")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("run", parents=[common],
                       help="prepare and submit a run")
    p.add_argument("config", help="the run configuration file")
    p.add_argument("--check", action="store_true",
                   help="validate only; write nothing, submit nothing")
    p.add_argument("--no-submit", action="store_true",
                   help="validate and generate scripts, but do not submit")
    p.add_argument("--submit-only", action="store_true",
                   help="submit existing scripts without regenerating")
    p.add_argument("--regenerate", action="store_true",
                   help="rebuild scripts before submitting")
    p.add_argument("--resume", action="store_true",
                   help="clear the stop marker and relaunch chains")
    p.add_argument("-w", "--width", type=int, metavar="N",
                   help="how many chains run concurrently")
    p.add_argument("--workers", type=int, metavar="N",
                   help="threads used to generate scripts")
    p.add_argument("--run-name", metavar="NAME",
                   help="override the run name from the configuration")
    p.add_argument("--strict", action="store_true",
                   help="refuse to proceed if any row fails validation")
    p.add_argument("--force", action="store_true",
                   help="discard an existing run of the same name")
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation --force would require")

    p = sub.add_parser("status", parents=[common],
                       help="how the run is going")
    p.add_argument("--row", metavar="SELECTOR", help="one row, as a table line")
    p.add_argument("--status", action="append", metavar="STATUS", dest="statuses",
                   help="only rows whose status starts with this; repeatable")
    p.add_argument("--stage", metavar="NAME", help="only rows at this stage")
    p.add_argument("--watch", action="store_true",
                   help="repaint until the run finishes")
    p.add_argument("--summary-only", action="store_true",
                   help="counts and warnings, without the table")
    p.add_argument("--metrics", action="store_true",
                   help="add throughput, per-stage timing, and a projection")
    p.add_argument("--all", action="store_true", dest="all_runs",
                   help="every run, one line each")
    p.add_argument("--prune-after", type=int, metavar="DAYS",
                   help="with --all, remove state for runs finished longer ago "
                        "than this")
    p.add_argument("--yes", action="store_true",
                   help="confirm a prune")

    p = sub.add_parser("show", parents=[common],
                       help="everything about one row")
    p.add_argument("--row", metavar="SELECTOR", help="the row to show")
    p.add_argument("--paths", action="store_true", help="only the artifact paths")
    p.add_argument("--stages", action="store_true", help="only the stage table")
    p.add_argument("--history", action="store_true",
                   help="every generation, not just the current one")
    p.add_argument("--output", action="store_true",
                   help="the scheduler's own log for the failing stage")
    p.add_argument("--stage", metavar="NAME", help="with --output, which stage")
    p.add_argument("--full", action="store_true", help="every section")
    p.add_argument("--invalid", action="store_true",
                   help="all rows that failed validation")

    p = sub.add_parser("rerun", parents=[common],
                       help="run rows or stages again")
    p.add_argument("--row", action="append", metavar="SELECTOR", dest="rows",
                   help="row to re-run; repeatable")
    p.add_argument("--status", action="append", metavar="STATUS", dest="statuses",
                   help="every row whose status starts with this; repeatable")
    p.add_argument("--set", action="append", metavar="COL=VALUE",
                   dest="assignments", help="change a value first; repeatable")
    p.add_argument("--stage", metavar="NAME", help="one stage only")
    p.add_argument("--stages", metavar="A,B", help="those stages, in order")
    p.add_argument("--from", metavar="NAME", dest="from_stage",
                   help="that stage and everything after it")
    p.add_argument("--chain", action="store_true",
                   help="resume chaining from these rows")
    p.add_argument("--regenerate", action="store_true",
                   help="rebuild scripts even without --set")
    p.add_argument("--fresh-handoff", action="store_true",
                   help="start the new generation with an empty handoff")
    p.add_argument("--force", action="store_true",
                   help="override the attempt cap, an active job, or completion")
    p.add_argument("--yes", action="store_true",
                   help="skip the typed confirmation")

    p = sub.add_parser("cancel", parents=[common], help="stop jobs")
    p.add_argument("--row", action="append", metavar="SELECTOR", dest="rows",
                   help="row to cancel; repeatable")
    p.add_argument("--status", action="append", metavar="STATUS", dest="statuses",
                   help="every row whose status starts with this; repeatable")
    p.add_argument("--stage", metavar="NAME", help="one stage only")
    p.add_argument("--all", action="store_true", dest="all_rows",
                   help="every active row, and stop the chain")
    p.add_argument("--stop", action="store_true",
                   help="stop the chain only; let running jobs finish")

    p = sub.add_parser("doctor", parents=[common],
                       help="reconcile against the scheduler")
    p.add_argument("--repair", action="store_true",
                   help="reset orphaned rows and restore the chain width")
    p.add_argument("--all", action="store_true", dest="all_runs",
                   help="check every run")
    p.add_argument("--check-fs", action="store_true",
                   help="verify the filesystem supports the claim protocol")

    p = sub.add_parser("logs", parents=[common], help="the run log")
    p.add_argument("--follow", action="store_true", help="tail as it grows")
    p.add_argument("--level", metavar="LEVEL", help="only entries at this level")
    p.add_argument("--stage", metavar="NAME", help="only entries about this stage")
    p.add_argument("--lines", type=int, default=40, metavar="N",
                   help="how many entries to show")

    p = sub.add_parser("export", parents=[common],
                       help="parameters and state as one delimited file")
    p.add_argument("-o", "--output", metavar="PATH", help="write here")
    p.add_argument("--status", action="append", metavar="STATUS", dest="statuses",
                   help="only rows whose status starts with this")

    return parser


_EPILOG = """\
commands:
  run CONFIG        prepare and submit; state-aware, so repeating it does
                    whatever remains
  status            how the run is going
  show --row R      everything about one row
  rerun --row R     run rows or stages again, with --set to change values
  cancel            stop jobs, and with --stop take no new work
  doctor            reconcile against the scheduler, --repair to fix
  logs              jobchain's record of the run
  export            parameters and state as one file

exit codes:
""" + "\n".join(f"  {code:<3} {name}" for code, name in sorted(EXIT_NAMES.items()))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, dispatch, and translate failures into exit codes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    configure_logging(verbosity=getattr(args, "verbose", 0))

    handler = _HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.error(f"unknown command {args.command}")
        return EXIT_USAGE

    try:
        return handler(args)
    except JobChainError as exc:
        get_logger().error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        get_logger().error("interrupted")
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover - depends on the consumer
        return EXIT_OK
    except Exception as exc:
        # Reaching here means an unexpected condition escaped the layers
        # below, which is a defect in this tool rather than bad input.
        get_logger().error(
            "internal error: %s. This is a defect in %s %s, not a problem with "
            "the input; the traceback below belongs in a bug report.",
            exc, PROGRAM, VERSION)
        import traceback
        traceback.print_exc()
        return EXIT_INTERNAL


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _emit(lines: Sequence[str]) -> None:
    for line in lines:
        print(line)


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _select_store(args: argparse.Namespace) -> Store:
    """Choose which run to act on.

    With one run it is used automatically. With several and no selection, the
    runs are listed and the command stops rather than guessing.
    """
    root = Store.discover_root()
    if root is None:
        raise StateError("no .jobchain directory found here or in any parent")

    selector = args.run_selector or os.environ.get("JOBCHAIN_RUN")
    names = Store.list_runs(root)
    if not names:
        raise StateError(f"no runs exist under {root}")
    if selector:
        if selector not in names:
            raise StateError(
                f"no run named '{selector}' under {root}; runs are "
                + ", ".join(names))
        return Store(os.path.join(root, selector))
    if len(names) == 1:
        return Store(os.path.join(root, names[0]))

    _emit([f"{len(names)} runs exist; specify one with --run", ""])
    _emit(report.render_run_list(root, [_run_summary(root, n) for n in names]))
    raise UsageError("several runs exist and none was selected")


def _run_summary(root: str, name: str) -> Dict[str, Any]:
    """One line of facts about a run, for the selection listing."""
    store = Store(os.path.join(root, name))
    try:
        rows = store.load_rows()
        config = store.load_config()
    except JobChainError:
        return {"name": name, "rows": 0, "done": 0, "failed": 0, "active": 0,
                "started": "-"}
    counts = report.summarize(rows)
    return {
        "name": name,
        "rows": len(rows),
        "done": counts.get(DONE, 0),
        "failed": counts.get("failed", 0) + counts.get("cancelled", 0),
        "active": sum(1 for r in rows if r.status in ACTIVE or r.status == "RUNNING"),
        "started": str(config.get("created_at", "-"))[:16],
    }


def _open(args: argparse.Namespace) -> operations.PreparedRun:
    """Load a run from its captured configuration.

    Completion is checked here rather than in one command, so whichever
    command a person happens to run is the one that notices the run has
    finished. The check is cheap and idempotent: it writes the marker only on
    the transition into completion, and removes it as soon as anything is
    outstanding again.
    """
    store = _select_store(args)
    prepared = operations.open_run(store)
    if getattr(args, "dry_run", False):
        from .scheduler import NullScheduler
        prepared.scheduler = NullScheduler(prepared.config.scheduler)
    _attach_file_log(prepared, args)
    if not getattr(args, "dry_run", False):
        operations.check_completion(store, prepared.config.on_complete)
    return prepared


def _attach_file_log(prepared: operations.PreparedRun,
                     args: argparse.Namespace) -> None:
    """Send full detail to the run's log file as well as the console."""
    configure_logging(
        verbosity=getattr(args, "verbose", 0),
        log_file=prepared.store.log_path,
        terminal_level=getattr(args, "log_level", None)
        or prepared.config.terminal_level,
        file_level=getattr(args, "file_log_level", None)
        or prepared.config.file_level,
    )


def _resolve_rows(prepared: operations.PreparedRun, args: argparse.Namespace,
                  default_all_active: bool = False) -> List[Any]:
    """Resolve a row selection from identifiers or statuses."""
    store = prepared.store
    unique = prepared.schema.unique_fields
    field_names = getattr(prepared.schema, "field_names", None)
    selectors = getattr(args, "rows", None) or []
    statuses = getattr(args, "statuses", None) or []

    if selectors:
        return [store.resolve_row(s, unique, field_names) for s in selectors]
    rows = store.load_rows()
    if statuses:
        wanted = [s.lower() for s in statuses]
        return [r for r in rows
                if any(r.status.lower().startswith(w) for w in wanted)]
    if getattr(args, "all_rows", False):
        return [r for r in rows if r.current is not None and not r.is_terminal]
    if default_all_active:
        return [r for r in rows if r.current is not None and not r.is_terminal]
    raise UsageError("select rows with --row or --status")


def _confirm(prompt: str, expected: str, skip: bool) -> bool:
    """Ask for a typed confirmation, unless it was waived."""
    if skip:
        return True
    if not sys.stdin.isatty():
        _emit([prompt, "not a terminal, and --yes was not given; nothing done"])
        return False
    _emit([prompt])
    try:
        return input(f"Type '{expected}' to confirm: ").strip() == expected
    except EOFError:
        return False


class Progress:
    """A progress bar for script generation.

    A bar rather than a line per script: several hundred "wrote script"
    messages is noise. Individual paths still reach the log file at debug
    level, so the detail is available without flooding the console.
    """

    def __init__(self, enabled: bool = True, width: int = 40):
        self.enabled = enabled and sys.stderr.isatty()
        self.width = width
        self.total = 0
        self.done = 0
        self.started = 0.0

    def start(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.started = time.time()
        self._paint()

    def advance(self, count: int = 1) -> None:
        self.done += count
        self._paint()

    def finish(self) -> None:
        if self.enabled:
            self._paint()
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _paint(self) -> None:
        if not self.enabled or not self.total:
            return
        filled = int(self.width * self.done / self.total)
        bar = "#" * filled + "." * (self.width - filled)
        elapsed = time.time() - self.started
        sys.stderr.write(f"\r      [{bar}] {self.done}/{self.total}  {elapsed:.1f}s")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Prepare and submit a run, doing whatever remains."""
    overrides = {
        "width": args.width,
        "workers": args.workers,
        "run_name": args.run_name,
        "strict": True if args.strict else None,
    }
    config = load_config(args.config, overrides)
    root = Store.discover_root(os.path.dirname(os.path.abspath(args.config))) \
        or Store.root_for(config.params_path)
    store = Store(config.home(root))

    if store.exists() and args.force and not _confirm_discard(store, args.yes):
        return EXIT_USAGE

    configure_logging(verbosity=args.verbose,
                      log_file=None if args.check else store.log_path,
                      terminal_level=args.log_level or config.terminal_level,
                      file_level=args.file_log_level or config.file_level)

    if not args.check and not args.as_json:
        _emit(_render_header(config, store))

    progress = Progress(enabled=not args.as_json)
    result = operations.run(
        config, root=root, check_only=args.check, no_submit=args.no_submit,
        submit_only=args.submit_only, regenerate=args.regenerate,
        resume=args.resume, force=args.force, dry_run=args.dry_run,
        progress=progress,
    )

    if not args.check and not args.dry_run:
        operations.check_completion(result.store, config.on_complete)

    if args.as_json:
        _emit_json({
            "run": result.store.name,
            "home": result.store.home,
            "phase": result.phase,
            "rows_created": result.rows_created,
            "rows_invalid": result.rows_invalid,
            "scripts": result.scripts_written,
            "submitted": [{"row": r, "jobs": dict(j)} for r, j in result.submitted],
            "failures": result.failures,
            "scan": result.scan_report.to_dict() if result.scan_report else None,
        })
        return EXIT_OK if result.scan_report is None or result.scan_report.ok else 3

    return _report_run(result, args)


def _render_header(config: RunConfig, store: Store) -> List[str]:
    """The settings block printed before work begins."""
    shape = "single job per row" if config.pipeline_source is None else "pipeline"
    return [
        f"{PROGRAM} {VERSION}   run '{config.name}'",
        "",
        f"  shape       {shape}",
        f"  config      {config.source_path}",
        f"  params      {config.params_path}",
        f"  scheduler   {config.scheduler}",
        f"  width       {config.width}",
        f"  home        {store.home}",
        "",
    ]


def _report_run(result: operations.RunResult, args: argparse.Namespace) -> int:
    """Render the outcome of a run command."""
    report_lines: List[str] = []
    if result.scan_report is not None:
        report_lines += format_report(result.scan_report)

    if result.phase == "check":
        _emit(report_lines)
        return EXIT_OK if result.scan_report and result.scan_report.ok else 3

    if result.scripts_written:
        report_lines.append(f"generated {result.scripts_written} script(s)")
    for name, jobs in result.submitted:
        report_lines.append(
            f"row {name} submitted: " + ", ".join(f"{s} {j}" for s, j in jobs))
    for name, reason in result.failures:
        report_lines.append(f"row {name} failed to submit: {reason}")
    if result.exhausted and not result.submitted:
        report_lines.append("no rows are available to claim")

    if result.rows_invalid:
        report_lines += [
            "",
            (f"!  {result.rows_invalid} row(s) were NOT submitted: they failed "
             f"validation"),
        ]
    _emit(report_lines)
    return EXIT_OK if not result.failures else 7


def _confirm_discard(store: Store, skip: bool) -> bool:
    """Confirm discarding an existing run that holds results."""
    try:
        rows = store.load_rows()
    except JobChainError:
        return True
    finished = [r for r in rows if r.is_terminal and r.valid]
    active = [r for r in rows if r.current is not None and not r.is_terminal]
    if not finished and not active:
        return True
    return _confirm(
        f"This permanently deletes run '{store.name}': {len(finished)} finished "
        f"row(s), {len(active)} active job(s), and all logs.",
        store.name, skip)


def cmd_status(args: argparse.Namespace) -> int:
    """Show how the run is going, always as a table."""
    if args.all_runs:
        return _status_all(args)

    prepared = _open(args)
    store = prepared.store
    rows = store.load_rows()

    if args.row:
        rows = [store.resolve_row(args.row, prepared.schema.unique_fields,
                                  getattr(prepared.schema, "field_names", None))]

    views = report.filter_views(report.build_views(rows), args.statuses, args.stage)
    counts = report.summarize(store.load_rows())
    config = store.load_config()

    live = sum(1 for r in store.load_rows()
               if r.current is not None and not r.is_terminal)
    metrics = report.compute_metrics(store.load_rows(), live,
                                     int(config.get("width", 1)))

    if args.as_json:
        payload = {"run": store.name, "home": store.home, "counts": counts,
                   "stopped": store.stopped,
                   "rows": report.views_to_dicts(views)}
        if args.metrics:
            payload["metrics"] = metrics.to_dict()
        _emit_json(payload)
        return EXIT_OK

    if args.watch:
        return _watch(prepared, args)

    _emit(_status_body(prepared, rows, views, counts, metrics, args))
    return EXIT_OK


def _status_body(prepared: operations.PreparedRun, rows: Sequence[Any],
                 views: Sequence[Any], counts: Dict[str, int],
                 metrics: Any, args: argparse.Namespace) -> List[str]:
    store = prepared.store
    all_rows = store.load_rows()
    lines = [f"run '{store.name}'"
             + (f"   {prepared.config.description}"
                if prepared.config.description else ""),
             f"home {store.home}", ""]
    lines += report.render_summary(counts, len(all_rows))
    warnings = report.render_warnings(all_rows, metrics.live_chains,
                                      metrics.target_width, store.stopped)
    if warnings:
        lines += ["", *warnings]
    if args.metrics:
        lines += ["", *report.render_metrics(metrics)]
    if not args.summary_only:
        lines += ["", *report.render_table(views)]
    return lines


def _watch(prepared: operations.PreparedRun, args: argparse.Namespace) -> int:
    """Repaint the status view until the run finishes or is interrupted."""
    store = prepared.store
    try:
        while True:
            rows = store.load_rows()
            views = report.filter_views(report.build_views(rows), args.statuses,
                                        args.stage)
            counts = report.summarize(rows)
            live = sum(1 for r in rows if r.current is not None and not r.is_terminal)
            metrics = report.compute_metrics(
                rows, live, int(store.load_config().get("width", 1)))
            sys.stdout.write("\033[H\033[J")
            _emit([time.strftime("%Y-%m-%d %H:%M:%S"), ""])
            _emit(_status_body(prepared, rows, views, counts, metrics, args))
            sys.stdout.flush()
            if not [r for r in rows if r.valid and not r.is_terminal]:
                _emit(["", "run complete"])
                return EXIT_OK
            time.sleep(WATCH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        _emit(["", "stopped watching; the run continues"])
        return EXIT_OK


def _status_all(args: argparse.Namespace) -> int:
    """One line per run, and optionally prune the long-finished ones."""
    root = Store.discover_root()
    if root is None:
        raise StateError("no .jobchain directory found here or in any parent")
    names = Store.list_runs(root)

    if args.prune_after is not None:
        return _prune_runs(root, names, args.prune_after, args.yes, args.as_json)

    entries = [_run_summary(root, name) for name in names]
    if args.as_json:
        _emit_json({"root": root, "runs": entries})
        return EXIT_OK
    if not entries:
        _emit(["no runs exist"])
        return EXIT_OK
    _emit(report.render_run_list(root, entries))
    return EXIT_OK


def _prune_runs(root: str, names: Sequence[str], days: int, yes: bool,
                as_json: bool) -> int:
    """Remove state for runs where everything finished more than N days ago.

    Only runs with nothing outstanding are eligible, and nothing is ever
    removed without saying which runs and asking, because a state directory
    is the only record of what a run did.
    """
    cutoff = time.time() - days * 86400
    eligible: List[str] = []
    for name in names:
        store = Store(os.path.join(root, name))
        if not os.path.isfile(store.done_path):
            continue          # still outstanding, or never completed
        if os.path.getmtime(store.done_path) > cutoff:
            continue          # finished too recently
        eligible.append(name)

    if as_json:
        _emit_json({"eligible": eligible, "pruned": bool(yes)})
        if yes:
            for name in eligible:
                Store(os.path.join(root, name)).destroy()
        return EXIT_OK

    if not eligible:
        _emit([f"no runs finished more than {days} day(s) ago"])
        return EXIT_OK

    _emit([f"{len(eligible)} run(s) finished more than {days} day(s) ago:"]
          + [f"  {name}" for name in eligible])
    if not yes:
        _emit(["", "nothing was removed; pass --yes to remove them"])
        return EXIT_USAGE
    for name in eligible:
        Store(os.path.join(root, name)).destroy()
    _emit([f"removed {len(eligible)} run(s)"])
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    """Show everything about one row, always as sections."""
    prepared = _open(args)
    store = prepared.store

    if args.invalid:
        rows = store.load_rows()
        if args.as_json:
            _emit_json([{"row": r.name, "row_id": r.row_id, "line": r.line_num,
                         "reasons": r.invalid_reasons}
                        for r in rows if not r.valid])
            return EXIT_OK
        _emit(report.render_invalid(rows))
        return EXIT_OK

    if not args.row:
        raise UsageError("show needs --row, or --invalid")

    row = store.resolve_row(args.row, prepared.schema.unique_fields,
                            getattr(prepared.schema, "field_names", None))

    if args.output:
        return _show_output(prepared, row, args.stage)

    sections: List[str] = []
    if args.paths:
        sections.append("paths")
    if args.stages:
        sections.append("stages")
    if args.full:
        sections = []

    if args.as_json:
        run = row.current
        _emit_json({
            "row": row.name, "row_id": row.row_id, "line": row.line_num,
            "status": row.status, "generation": row.generation,
            "valid": row.valid, "invalid_reasons": row.invalid_reasons,
            "params": row.params, "work_dir": row.work_dir,
            "stages": [{"name": s.name, "status": s.status, "jobid": s.jobid,
                        "depends": s.depends, "script": s.script,
                        "resources": s.resources, "error": s.error}
                       for s in (run.stages if run else [])],
            "handoff": run.handoff if run else {},
        })
        return EXIT_OK

    _emit(report.render_show(row, store, sections or None, history=args.history))
    return EXIT_OK


def _show_output(prepared: operations.PreparedRun, row: Any,
                 stage: Optional[str]) -> int:
    """Print the scheduler's own log for a stage."""
    store = prepared.store
    name = stage or row.stage_reached
    if not name:
        raise StateError(f"row {row.name} has not run any stage yet")
    directory = os.path.join(store.home, "logs", row.name)
    candidates = []
    if os.path.isdir(directory):
        candidates = [os.path.join(directory, f) for f in sorted(os.listdir(directory))
                      if f.startswith(name)]
    if not candidates:
        raise StateError(
            f"no scheduler output found for row {row.name} stage {name} in "
            f"{directory}")
    for path in candidates:
        _emit([f"--- {path} ---"])
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            sys.stdout.write(handle.read())
    return EXIT_OK


def cmd_rerun(args: argparse.Namespace) -> int:
    """Run rows or stages again, optionally with changed values."""
    prepared = _open(args)
    rows = _resolve_rows(prepared, args)

    assignments: Dict[str, str] = {}
    for item in args.assignments or []:
        if "=" not in item:
            raise UsageError(f"--set expects COLUMN=VALUE, got '{item}'")
        key, _, value = item.partition("=")
        assignments[key.strip()] = value

    stages = [args.stage] if args.stage else (
        args.stages.split(",") if args.stages else None)

    plan = operations.plan_rerun(prepared, rows, assignments=assignments,
                                 stages=stages, from_stage=args.from_stage,
                                 force=args.force)

    if plan.needs_confirmation:
        if not args.force:
            _emit(_render_completed_warning(plan.needs_confirmation))
            raise ConflictError(
                f"{len(plan.needs_confirmation)} completed row(s) still have "
                f"output; --force is required to re-run them")
        _emit(_render_completed_warning(plan.needs_confirmation))
        expected = plan.needs_confirmation[0][0].row_id \
            if len(plan.needs_confirmation) == 1 else prepared.store.name
        if not _confirm("Re-running may overwrite the output above.",
                        expected, args.yes):
            return EXIT_USAGE

    result = operations.execute_rerun(
        prepared, plan, assignments=assignments, regenerate=args.regenerate,
        chain=args.chain, fresh_handoff=args.fresh_handoff, dry_run=args.dry_run)

    if not args.dry_run:
        operations.check_completion(prepared.store,
                                    prepared.config.on_complete)

    if args.as_json:
        _emit_json({"rows": result.rows, "regenerated": result.regenerated,
                    "submitted": [{"row": r, "jobs": dict(j)}
                                  for r, j in result.submitted],
                    "skipped": result.skipped, "failures": result.failures})
        return EXIT_OK

    lines = []
    for name in result.rows:
        lines.append(f"row {name} re-queued")
    for name, reason in result.skipped:
        lines.append(f"row {name} skipped: {reason}")
    for name, jobs in result.submitted:
        lines.append("row {} submitted: {}".format(
            name, ", ".join(f"{stage} {job}" for stage, job in jobs)))
    for name, reason in result.failures:
        lines.append(f"row {name} failed to submit: {reason}")
    if not lines:
        lines.append("no rows matched")
    _emit(lines)
    return EXIT_OK if not result.failures else 7


def _render_completed_warning(entries: Sequence[Any]) -> List[str]:
    """Report output directories a rerun might overwrite."""
    lines: List[str] = []
    for row, directories in entries:
        lines.append("")
        lines.append(f"row {row.name} ({row.row_id}) completed successfully."
                     f" Output directories still exist:")
        for path, count, size in directories:
            lines.append(f"  {path}   {count} files, {report._format_size(size)}")
    lines.append("")
    lines.append("These are directories jobchain knows about."
                 " Stages may write elsewhere.")
    return lines


def cmd_cancel(args: argparse.Namespace) -> int:
    """Stop jobs, and optionally stop the chain from taking new work."""
    prepared = _open(args)

    if args.stop and not (args.rows or args.statuses or args.all_rows):
        if not args.dry_run:
            prepared.store.stop("stopped by request")
        _emit([(f"run '{prepared.store.name}' stopped: no further rows will "
                f"be claimed")])
        return EXIT_OK

    rows = _resolve_rows(prepared, args, default_all_active=args.all_rows)
    result = operations.cancel(prepared, rows, stage=args.stage,
                               stop=args.stop or args.all_rows,
                               dry_run=args.dry_run)

    if args.as_json:
        _emit_json({"cancelled": [{"row": r, "jobs": j} for r, j in result.cancelled],
                    "stopped": result.stopped, "skipped": result.skipped})
        return EXIT_OK

    lines = [f"row {name}: cancelled {len(jobs)} job(s)"
             for name, jobs in result.cancelled]
    for name, reason in result.skipped:
        lines.append(f"row {name} skipped: {reason}")
    if result.stopped:
        lines.append("the chain is stopped; no further rows will be claimed")
    _emit(lines or ["no active jobs matched"])
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Reconcile recorded state against the scheduler."""
    if args.check_fs:
        return _check_filesystem(args)

    if args.all_runs:
        root = Store.discover_root()
        if root is None:
            raise StateError("no .jobchain directory found")
        results = []
        for name in Store.list_runs(root):
            store = Store(os.path.join(root, name))
            prepared = operations.open_run(store)
            results.append(operations.doctor(prepared, repair=args.repair,
                                             dry_run=args.dry_run))
        if args.as_json:
            _emit_json([_doctor_payload(r) for r in results])
            return EXIT_OK
        for result in results:
            repaired = sum(1 for f in result.findings if f.repaired)
            _emit([(f"{result.run:<24} {len(result.findings)} finding(s), "
                    f"{repaired} repaired, "
                    f"{result.live_chains}/{result.target_width} chains")])
        return EXIT_OK if all(r.ok for r in results) else 6

    prepared = _open(args)
    result = operations.doctor(prepared, repair=args.repair, dry_run=args.dry_run)

    if args.as_json:
        _emit_json(_doctor_payload(result))
        return EXIT_OK if result.ok or args.repair else 6

    _emit(_render_doctor(result))
    return EXIT_OK if result.ok or args.repair else 6


def _doctor_payload(result: operations.DoctorResult) -> Dict[str, Any]:
    return {
        "run": result.run,
        "live_chains": result.live_chains,
        "target_width": result.target_width,
        "stopped": result.stopped,
        "findings": [{"row": f.row, "detail": f.detail, "repaired": f.repaired}
                     for f in result.findings],
        "relaunched": [{"row": r, "jobs": dict(j)} for r, j in result.relaunched],
        "environment": result.environment,
    }


def _render_doctor(result: operations.DoctorResult) -> List[str]:
    lines = [f"run '{result.run}'", ""]
    shortfall = result.target_width - result.live_chains
    lines.append(f"chains       {result.live_chains} live, configured width "
                 f"{result.target_width}"
                 + (f"        SHORTFALL {shortfall}" if shortfall > 0 else ""))
    lines.append(f"rows         {result.total_rows} total, "
                 f"{result.finished_rows} finished, {result.active_rows} active, "
                 f"{result.pending_rows} pending")
    lines.append("")
    if not result.findings:
        lines.append("no problems found")
    else:
        lines.append(f"findings ({len(result.findings)})")
        for finding in result.findings:
            mark = "repaired" if finding.repaired else "found"
            where = f"row {finding.row} " if finding.row != "-" else ""
            lines.append(f"  [{mark}] {where}{finding.detail}")
    if result.relaunched:
        lines.append("")
        lines.append(f"relaunched {len(result.relaunched)} chain(s): "
                     + ", ".join(row for row, _ in result.relaunched))
    lines += ["", "environment"]
    for key in sorted(result.environment):
        lines.append(f"  {key:<16} {result.environment[key]}")
    return lines


def _check_filesystem(args: argparse.Namespace) -> int:
    """Verify the filesystem supports the claim protocol."""
    root = Store.discover_root() or os.path.join(os.getcwd(), ".jobchain")
    store = Store(os.path.join(root, "_check"))
    ok, output = store.selftest()
    store.destroy()
    if args.as_json:
        _emit_json({"ok": ok, "output": output})
        return EXIT_OK if ok else 8
    _emit([output, "",
           "this filesystem can host a run" if ok
           else "this filesystem cannot safely host a run"])
    return EXIT_OK if ok else 8


def cmd_logs(args: argparse.Namespace) -> int:
    """Show jobchain's own record of the run."""
    prepared = _open(args)
    store = prepared.store
    path = store.log_path

    if not os.path.isfile(path):
        _emit(["no log entries yet"])
        return EXIT_OK

    def matching(lines: Sequence[str]) -> List[str]:
        result = list(lines)
        if args.level:
            result = [line for line in result
                      if args.level.upper() in line.split(maxsplit=3)[:3]]
        if args.stage:
            result = [line for line in result if f"stage {args.stage}" in line]
        return result

    if args.follow:
        return _follow(path, matching)

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    selected = matching(lines)[-args.lines:]
    if args.as_json:
        _emit_json({"entries": selected})
        return EXIT_OK
    _emit(selected or ["no matching entries"])
    return EXIT_OK


def _follow(path: str, matching: Any) -> int:
    """Tail the run log until interrupted."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if line:
                    for shown in matching([line.rstrip("\n")]):
                        print(shown)
                        sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """Write parameters and state as one delimited file."""
    prepared = _open(args)
    rows = prepared.store.load_rows()
    if args.statuses:
        wanted = [s.lower() for s in args.statuses]
        rows = [r for r in rows
                if any(r.status.lower().startswith(w) for w in wanted)]

    if args.as_json:
        _emit_json(report.views_to_dicts(report.build_views(rows)))
        return EXIT_OK

    lines = report.export_rows(prepared.schema, rows)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
        _emit([f"wrote {len(rows)} row(s) to {args.output}"])
    else:
        _emit(lines)
    return EXIT_OK


_HANDLERS = {
    "run": cmd_run,
    "status": cmd_status,
    "show": cmd_show,
    "rerun": cmd_rerun,
    "cancel": cmd_cancel,
    "doctor": cmd_doctor,
    "logs": cmd_logs,
    "export": cmd_export,
}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
