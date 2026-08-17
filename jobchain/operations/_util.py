"""Small helpers shared across the operations subpackage.

Nothing here is specific to preparing, submitting, rerunning, cancelling, or
diagnosing a run -- both concerns just happen to need a content digest and an
atomically-written JSON file.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any


def _digest(path: str) -> str:
    """Content digest of a file, used to detect external edits."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def _write_json_file(path: str, payload: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
