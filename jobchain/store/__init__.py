"""On-disk state for a run.

Every execution owns a directory named for the run, so two unrelated runs in
the same working directory never interact::

    .jobchain/
      solver-production/
        config.original.yaml  config.final.yaml  jobchain.log
        rows.idx  events.log  done.json  stopped
        rows/000123/
          meta.json  env  gen  manifest  hold
          run-1/  claim  status  status.<stage>  jobid.<stage>  handoff  timeline

Two rules make concurrent access safe without a lock on the hot path.
Claiming a row is a single ``mkdir`` of its generation directory, which
succeeds for exactly one caller. Editing a row takes it out of circulation
with a hold file first, so a claimer never sees a half-written row.

Claiming is performed by the compiled helper rather than reimplemented here,
so there is one implementation of the protocol and no chance of the two
drifting apart.

This package splits the module's own documented concerns apart:

* ``model`` -- RowStatus and the RowState/RunState/StageState dataclasses:
  what a row's history looks like, independent of how it is stored.
* ``io`` -- atomic file primitives, shell-fragment rendering, and the small
  row-lookup helpers that don't belong to any one class.
* ``node`` -- locating the compute-node helper binary.
* ``core`` -- the Store class itself, which ties the above together.

Everything reachable as ``jobchain.store.X`` before the split still is.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import VERSION, NodeHelperError, StateError, get_logger, trace
from .core import Store
from .io import (
    _column_value,
    _hostname,
    _pad,
    _parse_assignments,
    _parse_handoff,
    _read_json,
    _read_lines,
    _read_optional,
    _read_text,
    _write_json,
    _write_text,
    render_env,
    row_name,
    shell_quote,
)
from .model import (
    ACTIVE,
    CANCELLED,
    CLAIMED,
    DEFAULT_ROOT,
    DONE,
    FAILED,
    INVALID,
    NODE_BINARY_NAME,
    PENDING,
    QUEUED,
    RUNNING,
    TERMINAL,
    RowState,
    RowStatus,
    RunState,
    StageState,
    _code_of,
)
from .node import find_node_binary

#: The complete pre-split public surface of jobchain.store, private helpers
#: included: tests reach jobchain.store.<name> directly (both as attribute
#: calls and as mock.patch targets against os/shutil) rather than through a
#: submodule. Listed explicitly so every name's re-export is intentional,
#: not an artifact ruff's "unused import" check would otherwise flag.
__all__ = [
    "ACTIVE", "Any", "CANCELLED", "CLAIMED", "DEFAULT_ROOT", "DONE", "Dict",
    "Enum", "FAILED", "INVALID", "List", "NODE_BINARY_NAME", "NodeHelperError",
    "Optional", "PENDING", "QUEUED", "RUNNING", "RowState", "RowStatus",
    "RunState", "Sequence", "StageState", "StateError", "Store", "TERMINAL",
    "Tuple", "VERSION", "_code_of", "_column_value", "_hostname", "_pad",
    "_parse_assignments", "_parse_handoff", "_read_json", "_read_lines",
    "_read_optional", "_read_text", "_write_json", "_write_text",
    "annotations", "dataclass", "dc_field", "find_node_binary", "get_logger",
    "json", "os", "render_env", "row_name", "shell_quote", "shutil",
    "subprocess", "time", "trace",
]
