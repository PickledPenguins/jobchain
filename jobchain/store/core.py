"""The Store: reads and writes the on-disk state of one run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core import VERSION, NodeHelperError, StateError, get_logger, trace
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
    shell_quote,
)
from .model import DEFAULT_ROOT, PENDING, RowState, RunState, StageState
from .node import find_node_binary


class Store:
    """Reads and writes the on-disk state of one run."""

    def __init__(self, home: str, node_binary: Optional[str] = None):
        self.home = os.path.abspath(home)
        self._node_binary = node_binary

    # -- discovery -------------------------------------------------------

    @staticmethod
    def root_for(params_path: str) -> str:
        """The .jobchain root beside a parameter file."""
        return os.path.join(os.path.dirname(os.path.abspath(params_path)),
                            DEFAULT_ROOT)

    @staticmethod
    def discover_root(start: Optional[str] = None) -> Optional[str]:
        """Find the nearest .jobchain directory, searching upward."""
        current = os.path.abspath(start or os.getcwd())
        while True:
            candidate = os.path.join(current, DEFAULT_ROOT)
            if os.path.isdir(candidate):
                return candidate
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    @staticmethod
    def list_runs(root: str) -> List[str]:
        """Names of every run under a .jobchain root, in name order."""
        if not os.path.isdir(root):
            return []
        return sorted(
            name for name in os.listdir(root)
            if os.path.isfile(os.path.join(root, name, "config.json"))
        )

    @property
    def name(self) -> str:
        return os.path.basename(self.home)

    @property
    def node_binary(self) -> str:
        if self._node_binary is None:
            self._node_binary = find_node_binary()
        return self._node_binary

    # -- paths -----------------------------------------------------------

    @property
    def rows_dir(self) -> str:
        return os.path.join(self.home, "rows")

    @property
    def index_path(self) -> str:
        return os.path.join(self.home, "rows.idx")

    @property
    def config_path(self) -> str:
        return os.path.join(self.home, "config.json")

    @property
    def events_path(self) -> str:
        return os.path.join(self.home, "events.log")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.home, "lock")

    @property
    def stop_path(self) -> str:
        return os.path.join(self.home, "stopped")

    @property
    def done_path(self) -> str:
        return os.path.join(self.home, "done.json")

    @property
    def completions_path(self) -> str:
        return os.path.join(self.home, "completions.log")

    @property
    def log_path(self) -> str:
        return os.path.join(self.home, "jobchain.log")

    def row_dir(self, name: str) -> str:
        return os.path.join(self.rows_dir, name)

    def run_dir(self, row_name: str, generation: int) -> str:
        return os.path.join(self.row_dir(row_name), f"run-{generation}")

    # -- lifecycle -------------------------------------------------------

    def exists(self) -> bool:
        return os.path.isfile(self.config_path)

    def require(self) -> None:
        if not self.exists():
            raise StateError(f"no run state found at {self.home}")

    def create(self, config: Dict[str, Any]) -> None:
        """Create the directory skeleton and record the run configuration."""
        os.makedirs(self.rows_dir, exist_ok=True)
        payload = dict(config)
        payload["version"] = VERSION
        payload["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(self.config_path, payload)

    def load_config(self) -> Dict[str, Any]:
        self.require()
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"could not read {self.config_path}: {exc}") from exc

    def update_config(self, **changes: Any) -> Dict[str, Any]:
        config = self.load_config()
        config.update(changes)
        _write_json(self.config_path, config)
        return config

    def destroy(self) -> None:
        if os.path.isdir(self.home):
            shutil.rmtree(self.home)

    def write_text_file(self, name: str, text: str) -> str:
        path = os.path.join(self.home, name)
        _write_text(path, text)
        return path

    # -- the setup lock --------------------------------------------------

    def acquire_lock(self) -> None:
        """Take the setup lock, which covers preparation only.

        Jobs never take this lock; it exists so two simultaneous preparations
        of the same run cannot interleave.
        """
        os.makedirs(self.home, exist_ok=True)
        try:
            os.mkdir(self.lock_path)
        except FileExistsError:
            owner = _read_text(os.path.join(self.lock_path, "owner"), "").strip()
            raise StateError(
                f"another jobchain process is preparing run '{self.name}'"
                + (f" ({owner})" if owner else "")
            ) from None
        _write_text(os.path.join(self.lock_path, "owner"),
                    f"host={_hostname()} pid={os.getpid()} "
                    f"since={time.strftime('%H:%M:%S')}\n")

    def release_lock(self) -> None:
        shutil.rmtree(self.lock_path, ignore_errors=True)

    # -- the stop marker -------------------------------------------------

    def stop(self, reason: str = "") -> None:
        """Stop the chain: no further rows are claimed."""
        _write_text(self.stop_path,
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")

    def resume(self) -> None:
        if os.path.exists(self.stop_path):
            os.unlink(self.stop_path)

    @property
    def stopped(self) -> bool:
        return os.path.exists(self.stop_path)

    # -- row creation ----------------------------------------------------

    def write_row(self, name: str, row_id: str, line_num: int, index: int,
                  params: Dict[str, Any], generation: int = 1,
                  valid: bool = True, invalid_reasons: Optional[List[str]] = None,
                  failure_id: str = "", work_dir: str = "",
                  raw_fields: Optional[Sequence[str]] = None) -> None:
        """Create or replace a row's identity and parameters.

        The generation file is written last: a claimer that reads a row
        mid-write must not see a new generation pointing at parameters that
        have not landed yet.
        """
        directory = self.row_dir(name)
        os.makedirs(directory, exist_ok=True)
        _write_json(os.path.join(directory, "meta.json"), {
            "name": name,
            "row_id": row_id,
            "line_num": line_num,
            "index": index,
            "params": params,
            "valid": valid,
            "invalid_reasons": list(invalid_reasons or []),
            "failure_id": failure_id,
            "work_dir": work_dir,
            "raw_fields": list(raw_fields or []),
        })
        _write_text(os.path.join(directory, "env"), render_env(params))
        _write_text(os.path.join(directory, "gen"), str(generation))

    def write_manifest(self, name: str,
                       entries: Sequence[Tuple[str, str, str]]) -> None:
        """Write the stage manifest a submitter reads.

        Three columns: stage name, dependency type, script path. Plain text so
        the compute-node side needs no YAML parser and no knowledge of the
        pipeline.
        """
        text = "".join(f"{stage}\t{depends or '-'}\t{script}\n"
                       for stage, depends, script in entries)
        _write_text(os.path.join(self.row_dir(name), "manifest"), text)

    def read_manifest(self, name: str) -> List[Tuple[str, str, str]]:
        text = _read_text(os.path.join(self.row_dir(name), "manifest"), "")
        entries = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) == 3:
                entries.append((parts[0], parts[1], parts[2]))
        return entries

    def write_index(self, names: Sequence[str]) -> None:
        _write_text(self.index_path, "".join(f"{n}\n" for n in names))

    def read_index(self) -> List[str]:
        self.require()
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                return [line.strip() for line in handle if line.strip()]
        except OSError as exc:
            raise StateError(f"could not read {self.index_path}: {exc}") from exc

    # -- holds and generations -------------------------------------------

    def hold(self, name: str) -> None:
        _write_text(os.path.join(self.row_dir(name), "hold"), "editing\n")

    def release(self, name: str) -> None:
        path = os.path.join(self.row_dir(name), "hold")
        if os.path.exists(path):
            os.unlink(path)

    def bump_generation(self, name: str) -> int:
        """Advance a row's generation, making it claimable again.

        Previous run directories are untouched, so every attempt stays
        inspectable afterwards. The new generation's handoff is seeded from
        the previous one, so a partial rerun still sees earlier stages'
        values.
        """
        row = self.load_row(name)
        generation = row.generation + 1
        _write_text(os.path.join(self.row_dir(name), "gen"), str(generation))
        # The done marker means "nothing outstanding right now", so it is
        # cleared the moment a row is re-queued rather than at the next check:
        # a rerun that finishes quickly would otherwise leave the previous
        # completion standing.
        self.clear_done()
        return generation

    def seed_handoff(self, name: str, values: Dict[str, str]) -> None:
        """Carry handoff values forward into the next generation.

        The seed lives beside the row rather than inside the next generation's
        directory: creating that directory is how a row is claimed, so writing
        into it early would make the row permanently unclaimable. Scripts
        source the seed first and the generation's own handoff second, so a
        value emitted this time overrides one carried forward.
        """
        path = os.path.join(self.row_dir(name), "handoff.seed")
        if not values:
            if os.path.exists(path):
                os.unlink(path)
            return
        _write_text(path, "".join(f"JC_OUT_{k}={shell_quote(v)}\n"
                                  f"export JC_OUT_{k}\n"
                                  for k, v in sorted(values.items())))

    def clear_done(self) -> None:
        """Remove the completion marker, because work is outstanding again."""
        if os.path.exists(self.done_path):
            os.unlink(self.done_path)

    def clear_handoff_seed(self, name: str) -> None:
        """Drop any carried-forward values, for a deliberately fresh start."""
        path = os.path.join(self.row_dir(name), "handoff.seed")
        if os.path.exists(path):
            os.unlink(path)

    # -- reading ---------------------------------------------------------

    def load_row(self, name: str) -> RowState:
        """Load one row's identity, parameters, and history."""
        directory = self.row_dir(name)
        meta_path = os.path.join(directory, "meta.json")
        if not os.path.isfile(meta_path):
            raise StateError(f"row '{name}' has no metadata at {meta_path}")
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"could not read {meta_path}: {exc}") from exc

        generation = int(_read_text(os.path.join(directory, "gen"), "1").strip() or "1")
        manifest = self.read_manifest(name)

        runs: List[RunState] = []
        for entry in sorted(os.listdir(directory)):
            if not entry.startswith("run-"):
                continue
            run_dir = os.path.join(directory, entry)
            if not os.path.isdir(run_dir):
                continue
            try:
                run_generation = int(entry[len("run-"):])
            except ValueError:
                continue  # not a generation directory this tool created
            runs.append(self._load_run(run_dir, run_generation, manifest))
        runs.sort(key=lambda r: r.generation)

        return RowState(
            name=meta["name"],
            row_id=str(meta.get("row_id", meta["name"])),
            line_num=int(meta.get("line_num", 0)),
            index=int(meta.get("index", 0)),
            params=meta.get("params", {}),
            generation=generation,
            runs=runs,
            held=os.path.exists(os.path.join(directory, "hold")),
            valid=bool(meta.get("valid", True)),
            invalid_reasons=list(meta.get("invalid_reasons", [])),
            failure_id=str(meta.get("failure_id", "")),
            work_dir=str(meta.get("work_dir", "")),
            raw_fields=list(meta.get("raw_fields", [])),
        )

    def _load_run(self, run_dir: str, generation: int,
                  manifest: Sequence[Tuple[str, str, str]]) -> RunState:
        """Load one generation's stage states and handoff values."""
        timeline = _read_lines(os.path.join(run_dir, "timeline"))
        # Values carried forward, then this generation's own, which win.
        handoff = _parse_handoff(_read_text(
            os.path.join(os.path.dirname(run_dir), "handoff.seed"), ""))
        handoff.update(_parse_handoff(
            _read_text(os.path.join(run_dir, "handoff"), "")))
        stages: List[StageState] = []
        for stage_name, depends, script in manifest:
            status = _read_text(
                os.path.join(run_dir, f"status.{stage_name}"), "").strip()
            stages.append(StageState(
                name=stage_name,
                status=status or PENDING,
                jobid=_read_optional(os.path.join(run_dir, f"jobid.{stage_name}")),
                error=_read_optional(os.path.join(run_dir, f"error.{stage_name}")),
                depends=depends if depends != "-" else "",
                script=script,
                resources=_read_json(
                    os.path.join(run_dir, f"resources.{stage_name}.json"), {}),
                timeline=[line for line in timeline
                          if f"stage={stage_name} " in line
                          or line.endswith(f"stage={stage_name}")],
            ))
        return RunState(
            generation=generation,
            claim=_read_optional(os.path.join(run_dir, "claim")),
            stages=stages,
            handoff=handoff,
        )

    def load_rows(self) -> List[RowState]:
        return [self.load_row(name) for name in self.read_index()]

    def resolve_row(self, identifier: str,
                    unique_fields: Optional[Sequence[str]] = None,
                    field_names: Optional[Sequence[str]] = None) -> RowState:
        """Find a row by state name, row number, source line, or a unique column.

        ``column=value`` is the form that needs no knowledge of jobchain's
        indexing: any column declared unique in the schema can name a row.
        ``field_names`` is the schema's raw column order, used to look up a
        column's value in a row that failed validation (which has no typed
        ``params``, only ``raw_fields``); without it, such a row can only be
        matched by its raw text position.
        """
        rows = self.load_rows()

        if "=" in identifier:
            column, _, wanted = identifier.partition("=")
            column = column.strip()
            if unique_fields is not None and column not in unique_fields:
                raise StateError(
                    f"column '{column}' is not unique, so it cannot name a row; "
                    f"unique columns are {list(unique_fields) or 'none'}"
                )
            matches = [r for r in rows
                       if _column_value(r, column, field_names) == wanted]
            if not matches:
                raise StateError(f"no row has {column}={wanted}")
            if len(matches) > 1:
                raise StateError(
                    f"{len(matches)} rows have {column}={wanted}; the column is "
                    f"not unique in this run"
                )
            return matches[0]

        if identifier.startswith("line:"):
            wanted = identifier[len("line:"):]
            if not wanted.isdigit():
                raise StateError(f"'{identifier}' is not a valid line reference")
            for row in rows:
                if row.line_num == int(wanted):
                    return row
            raise StateError(f"no row came from line {wanted}")

        by_name = {row.name: row for row in rows}
        if identifier in by_name:
            return by_name[identifier]
        if identifier.isdigit():
            padded = _pad(identifier)
            if padded in by_name:
                return by_name[padded]
        for row in rows:
            if row.row_id == identifier:
                return row
        raise StateError(f"no row matches '{identifier}'")

    # -- helper invocation -----------------------------------------------

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

    def write_resources(self, run_dir: str, stage: str,
                        resources: Dict[str, Any]) -> None:
        """Record what a stage requested, for later display."""
        os.makedirs(run_dir, exist_ok=True)
        _write_json(os.path.join(run_dir, f"resources.{stage}.json"),
                    {k: v for k, v in resources.items()
                     if v not in (None, "", [], {})})

    def _run_node(self, arguments: List[str]) -> subprocess.CompletedProcess:
        command = [self.node_binary, *arguments]
        trace("node helper: %s", " ".join(command))
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  check=False)
        except OSError as exc:
            raise NodeHelperError(
                f"could not execute {self.node_binary}: {exc}") from exc

    # -- reporting -------------------------------------------------------

    def read_events(self) -> List[str]:
        return _read_lines(self.events_path)

    def summary(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.load_rows():
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts
