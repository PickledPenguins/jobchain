"""Acting on a run: preparing it, launching it, correcting it, repairing it.

Everything here is a command a person issues against a run, and all of it
goes through the same store, so the ordering guarantees hold no matter which
path is taken.

Preparation is a single pass: normalize, validate, write row state, generate
scripts, submit. It is state-aware, so running the same command twice does
what remains rather than starting over.

Correction never mutates in place. A row is taken out of circulation with a
hold file, rewritten, and only then given a new generation, so a claimer sees
either the old generation with the old parameters or the new generation with
the new ones.

This package is organized by the lifecycle stage each piece belongs to:
``lifecycle`` (loading and the run/prepare pipeline), ``submit`` (talking to
the scheduler), ``rerun`` (correcting and resubmitting), ``cancellation``,
``reconcile`` (doctor), and ``completion``. Everything that was reachable as
``jobchain.operations.X`` before the split still is: this module re-exports
the full previous surface, so callers, tests, and any external code never
see the internal package boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config import RunConfig, expand_template, render_final_config, template_is_generation_aware
from ..core import (
    ConflictError,
    DataError,
    StateError,
    UsageError,
    get_logger,
    log_startup_summary,
    trace,
)
from ..parse import RowResult, ScanReport, normalize_file, scan, scan_row
from ..pipeline import Pipeline, load_pipeline_source, single_job_pipeline
from ..scheduler import (
    ALIVE,
    FINISHED,
    NullScheduler,
    RowContext,
    RunContext,
    Scheduler,
    SchedulerBackend,
    describe_environment,
    verify_script,
)
from ..schema import Schema, apply_base_dir, load_schema_source
from ..store import (
    ACTIVE,
    CANCELLED,
    DONE,
    FAILED,
    PENDING,
    QUEUED,
    RUNNING,
    TERMINAL,
    RowState,
    Store,
    row_name,
)
from ._util import _digest, _write_json_file
from .cancellation import CancelResult, cancel
from .completion import _read_completions, _run_hook, check_completion
from .lifecycle import (
    PreparedRun,
    RunResult,
    _check_inputs_unchanged,
    _context_for,
    _continue_existing,
    _create_row_state,
    _generate_scripts,
    _identifier_for,
    _pipeline_document,
    _prepare_fresh,
    _schema_document,
    _validate_only,
    open_run,
    prepare,
    run,
)
from .reconcile import DoctorResult, Finding, _check_params_digest, doctor
from .rerun import (
    RerunPlan,
    RerunResult,
    _apply_changes,
    _describe_changes,
    _directory_size,
    _existing_output,
    _raw_fields,
    _regenerate_row,
    _submit_selected,
    execute_rerun,
    plan_rerun,
)
from .submit import _record_submissions, _submit_chains, _submit_row

#: This package's actual public API: what a caller of jobchain.operations
#: wants. Everything reachable as jobchain.operations.X before the split
#: still is (see _INTERNAL_REEXPORTS below), but this -- not the raw
#: attribute list -- is what dir()-respecting tools (pydoc, Sphinx, help())
#: and `from jobchain.operations import *` should show.
__all__ = [
    "ACTIVE", "ALIVE", "CANCELLED", "CancelResult", "ConflictError", "DONE",
    "DataError", "DoctorResult", "FAILED", "FINISHED", "Finding",
    "NullScheduler", "PENDING", "Pipeline", "PreparedRun", "QUEUED",
    "RUNNING", "RerunPlan", "RerunResult", "RowContext", "RowResult",
    "RowState", "RunConfig", "RunContext", "RunResult", "ScanReport",
    "Scheduler", "SchedulerBackend", "Schema", "StateError", "Store",
    "TERMINAL", "UsageError", "apply_base_dir", "cancel", "check_completion",
    "describe_environment", "doctor", "execute_rerun", "expand_template",
    "load_pipeline_source", "load_schema_source", "log_startup_summary",
    "normalize_file", "open_run", "plan_rerun", "prepare",
    "render_final_config", "row_name", "run", "scan", "scan_row",
    "single_job_pipeline", "template_is_generation_aware", "verify_script",
]

#: Re-exported only so existing mock.patch targets and dir()-based tooling
#: keep working (e.g. patch("jobchain.operations.subprocess.run", ...)
#: called directly rather than through a submodule); these are
#: typing/stdlib re-exports and private implementation helpers, not part
#: of this package's declared API, so they're deliberately left out of
#: __all__ above.
_INTERNAL_REEXPORTS = (
    Any, Dict, List, Optional, Sequence, ThreadPoolExecutor, Tuple,
    as_completed, dataclass, dc_field, json, os, subprocess, time,
    get_logger, trace,
    _apply_changes, _check_inputs_unchanged, _check_params_digest,
    _context_for, _continue_existing, _create_row_state, _describe_changes,
    _digest, _directory_size, _existing_output, _generate_scripts,
    _identifier_for, _pipeline_document, _prepare_fresh, _raw_fields,
    _read_completions, _record_submissions, _regenerate_row, _run_hook,
    _schema_document, _submit_chains, _submit_row, _submit_selected,
    _validate_only, _write_json_file,
)
