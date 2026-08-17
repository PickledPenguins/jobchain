"""Command line front end.

This is the only package that parses arguments or prints for a person; every
layer beneath it returns data. It is also the single place where an exception
becomes an exit code, which upholds the rule that a traceback reaching the
terminal always indicates a defect in this tool rather than bad input.

Messages state what happened and stop. Suggested follow-up commands are
deliberately absent: hint text spread across dozens of messages drifts out of
step with the commands it names. Commands are documented once, in the help.

This package splits the module's own documented concerns apart:

* ``parser`` -- the argument grammar.
* ``support`` -- helpers shared by every command: output, run selection,
  progress, confirmation.
* ``commands`` -- the ``cmd_*`` functions themselves and their own private
  rendering helpers.
* ``entry`` -- the entry point (``main()``): dispatch, and where an
  exception becomes an exit code.

Everything reachable as ``jobchain.cli.X`` before the split still is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from .. import operations, report
from ..config import RunConfig, load_config
from ..core import (
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
from ..parse import format_report
from ..store import ACTIVE, DONE, Store
from .commands import (
    _HANDLERS,
    _check_filesystem,
    _confirm_discard,
    _doctor_payload,
    _follow,
    _prune_runs,
    _render_completed_warning,
    _render_doctor,
    _render_header,
    _report_run,
    _show_output,
    _status_all,
    _status_body,
    _watch,
    cmd_cancel,
    cmd_doctor,
    cmd_export,
    cmd_logs,
    cmd_rerun,
    cmd_run,
    cmd_show,
    cmd_status,
)
from .entry import main
from .parser import _EPILOG, build_parser
from .support import (
    PROGRAM,
    WATCH_INTERVAL_SECONDS,
    Progress,
    _attach_file_log,
    _confirm,
    _emit,
    _emit_json,
    _open,
    _resolve_rows,
    _run_summary,
    _select_store,
)

#: The complete pre-split public surface of jobchain.cli, private helpers
#: included: tests reach jobchain.cli.<name> directly (both as attribute
#: calls and as mock.patch targets) rather than through a submodule. Listed
#: explicitly so every name's re-export is intentional, not an artifact
#: ruff's "unused import" check would otherwise flag.
__all__ = [
    "ACTIVE", "Any", "ConflictError", "DONE", "Dict", "EXIT_INTERNAL",
    "EXIT_NAMES", "EXIT_OK", "EXIT_USAGE", "JobChainError", "List",
    "Optional", "PROGRAM", "Progress", "RunConfig", "Sequence", "StateError",
    "Store", "UsageError", "VERSION", "WATCH_INTERVAL_SECONDS", "_EPILOG",
    "_HANDLERS", "_attach_file_log", "_check_filesystem", "_confirm",
    "_confirm_discard", "_doctor_payload", "_emit", "_emit_json", "_follow",
    "_open", "_prune_runs", "_render_completed_warning", "_render_doctor",
    "_render_header", "_report_run", "_resolve_rows", "_run_summary",
    "_select_store", "_show_output", "_status_all", "_status_body", "_watch",
    "annotations", "argparse", "build_parser", "cmd_cancel", "cmd_doctor",
    "cmd_export", "cmd_logs", "cmd_rerun", "cmd_run", "cmd_show",
    "cmd_status", "configure_logging", "format_report", "get_logger", "json",
    "load_config", "main", "operations", "os", "report", "sys", "time",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
