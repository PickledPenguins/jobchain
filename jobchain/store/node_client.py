"""Talking to the compute-node helper: claim, mark, event, selftest.

Split out of Store so "read and write this run's on-disk state" and "shell
out to the compiled or shell compute-node helper" are two independently
testable responsibilities instead of one class doing both. Store still
exposes claim/mark/event/selftest/node_binary as thin delegating methods,
so every existing caller is unaffected; this is where their actual logic
lives now.
"""

from __future__ import annotations

import os
import subprocess
from typing import List, Optional, Tuple

from ..core import NodeHelperError, get_logger, trace
from .io import _parse_assignments
from .node import find_node_binary


class NodeHelperClient:
    """Invokes the compiled or shell compute-node helper for one run."""

    def __init__(self, home: str, node_binary: Optional[str] = None):
        self.home = home
        self._node_binary = node_binary

    @property
    def node_binary(self) -> str:
        if self._node_binary is None:
            self._node_binary = find_node_binary()
        return self._node_binary

    def claim(self) -> Optional[Tuple[str, str]]:
        """Claim the next eligible row via the compiled helper."""
        result = self._run_node(["claim", "--home", self.home])
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise NodeHelperError(
                f"claim failed ({result.returncode}): "
                f"{result.stderr.strip() or 'no diagnostic'}"
            )
        assignments = _parse_assignments(result.stdout)
        try:
            return assignments["JC_NEXT_ROW"], assignments["JC_NEXT_RUN"]
        except KeyError as exc:
            raise NodeHelperError(
                f"claim produced unexpected output: {result.stdout!r}") from exc

    def mark(self, run_dir: str, stage: str, status: Optional[str] = None,
             jobid: Optional[str] = None, error: Optional[str] = None) -> None:
        """Record a stage's status, its job id, or both.

        Passing a job id without a status records only the id. A submitter
        must do that: by the time the submit command returns, the job may
        already be running and may have written its own status, which must
        not be overwritten.
        """
        command = ["mark", "--run", run_dir, "--stage", stage]
        if status:
            command += ["--status", status]
        if jobid:
            command += ["--jobid", jobid]
        if error:
            command += ["--error", error]
        result = self._run_node(command)
        if result.returncode != 0:
            raise NodeHelperError(
                f"mark failed ({result.returncode}): "
                f"{result.stderr.strip() or 'no diagnostic'}"
            )

    def event(self, message: str) -> None:
        """Append a message to the run's event log."""
        result = self._run_node(["event", "--home", self.home, "--message", message])
        if result.returncode != 0:
            get_logger().warning("could not write event log: %s",
                                 result.stderr.strip())

    def selftest(self) -> Tuple[bool, str]:
        """Verify the filesystem supports the claim protocol."""
        os.makedirs(self.home, exist_ok=True)
        result = self._run_node(["selftest", "--home", self.home])
        return result.returncode == 0, (result.stdout + result.stderr).strip()

    def _run_node(self, arguments: List[str]) -> subprocess.CompletedProcess:
        command = [self.node_binary, *arguments]
        trace("node helper: %s", " ".join(command))
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  check=False)
        except OSError as exc:
            raise NodeHelperError(
                f"could not execute {self.node_binary}: {exc}") from exc
