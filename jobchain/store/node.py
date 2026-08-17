"""Locating the compute-node helper binary."""

from __future__ import annotations

import os
import shutil
from typing import List, Optional, Tuple

from ..core import NodeHelperError, trace
from .model import NODE_BINARY_NAME


def find_node_binary(explicit: Optional[str] = None) -> str:
    """Locate the compute-node helper.

    Search order is the explicit path, then JOBCHAIN_NODE, then a bin
    directory alongside this package, then PATH. The failure names every
    place that was tried, because a missing binary is the most likely
    first-run problem.
    """
    candidates: List[Tuple[str, Optional[str]]] = [
        ("--node-binary", explicit),
        ("JOBCHAIN_NODE", os.environ.get("JOBCHAIN_NODE")),
        ("alongside the package",
         os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                      os.path.abspath(__file__)))),
                      "bin", NODE_BINARY_NAME)),
        ("PATH", shutil.which(NODE_BINARY_NAME)),
    ]
    tried: List[str] = []
    for origin, path in candidates:
        if not path:
            tried.append(f"{origin} (unset)")
            continue
        if os.path.isfile(path) and os.access(path, os.X_OK):
            trace("using node helper from %s: %s", origin, path)
            return os.path.abspath(path)
        tried.append(f"{origin}: {path}")
    raise NodeHelperError(
        "the helper 'jobchain-node' could not be found; tried " + "; ".join(tried)
    )
