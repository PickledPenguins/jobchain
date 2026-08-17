"""The command line grammar."""

from __future__ import annotations

import argparse

from ..core import EXIT_NAMES, VERSION
from .support import PROGRAM


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
