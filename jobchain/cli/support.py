"""Small helpers shared by every command: output, run selection, progress."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Sequence

from .. import operations, report
from ..core import JobChainError, StateError, UsageError, configure_logging
from ..store import ACTIVE, DONE, Store

PROGRAM = "jobchain"
WATCH_INTERVAL_SECONDS = 5.0


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
        from ..scheduler import NullScheduler
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
