"""Completion detection and the on_complete hook."""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from ..core import get_logger
from ..store import DONE, Store
from ._util import _write_json_file


def check_completion(store: Store, on_complete: str = "") -> Optional[Dict[str, Any]]:
    """Write or remove the done marker, and run the completion hook.

    The marker is present only while nothing is outstanding, so a rerun
    removes it immediately. A counter distinguishes the first completion from
    one reached after corrections.
    """
    rows = store.load_rows()
    if not rows:
        return None
    outstanding = [row for row in rows if row.valid and not row.is_terminal]

    if outstanding:
        if os.path.exists(store.done_path):
            os.unlink(store.done_path)
        return None

    if os.path.exists(store.done_path):
        return None  # already recorded; nothing has changed

    previous = _read_completions(store)
    payload: Dict[str, Any] = {
        "run": store.name,
        "completion": len(previous) + 1,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "first_completed_at": previous[0] if previous
        else time.strftime("%Y-%m-%dT%H:%M:%S"),
        "work_dirs": sorted({r.work_dir for r in rows if r.work_dir})[:20],
    }
    counts: Dict[str, int] = {
        "total": len(rows),
        "done": sum(1 for r in rows if r.status == DONE),
        "failed": sum(1 for r in rows if r.status.startswith("failed.") and r.valid),
        "invalid": sum(1 for r in rows if not r.valid),
    }
    payload["rows"] = counts
    _write_json_file(store.done_path, payload)
    with open(store.completions_path, "a", encoding="utf-8") as handle:
        handle.write(f"{payload['completed_at']} completion="
                     f"{payload['completion']} done={counts['done']} "
                     f"failed={counts['failed']}\n")
    counts = payload["rows"]
    get_logger().info("run complete (completion %d): %d done, %d failed, "
                      "%d invalid", payload["completion"], counts["done"],
                      counts["failed"], counts["invalid"])

    if on_complete:
        _run_hook(store, on_complete, payload)
    return payload


def _read_completions(store: Store) -> List[str]:
    if not os.path.isfile(store.completions_path):
        return []
    with open(store.completions_path, "r", encoding="utf-8") as handle:
        return [line.split()[0] for line in handle if line.strip()]


def _run_hook(store: Store, command: str, payload: Dict[str, Any]) -> None:
    """Run the completion hook, never letting it fail the run."""
    counts: Dict[str, int] = dict(payload["rows"])  # type: ignore[arg-type]
    environment = dict(os.environ)
    environment.update({
        "JC_RUN_NAME": store.name,
        "JC_HOME": store.home,
        "JC_COMPLETION": str(payload["completion"]),
        "JC_ROWS_DONE": str(counts["done"]),
        "JC_ROWS_FAILED": str(counts["failed"]),
    })
    expanded = command.replace("{run.home}", store.home).replace(
        "{run.name}", store.name)
    try:
        completed = subprocess.run(expanded, shell=True, env=environment,
                                   capture_output=True, text=True, timeout=300)
        if completed.returncode != 0:
            get_logger().warning("on_complete exited %d: %s",
                                 completed.returncode, completed.stderr.strip())
    except Exception as exc:
        get_logger().warning("on_complete could not be run: %s", exc)
