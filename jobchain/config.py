"""The run configuration: one file describes a whole run.

A run configuration names the run, points at the parameter file, carries the
schema and pipeline either inline or by path, and holds jobchain's own
settings. Command-line options override it, and the merged result is written
back into the run directory so that a run can always be reproduced from what
is on disk.

There are deliberately no ``--schema`` or ``--pipeline`` options. If those
live in separate files, their paths belong in the configuration, which keeps
one file as the complete description of a run.
"""

from __future__ import annotations

import getpass
import os
import re
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Dict, List, Optional, Tuple

from .core import VERSION, ConfigError, UsageError

#: Settings that may be overridden on the command line, and their types.
OVERRIDABLE = {
    "width": int,
    "workers": int,
    "max_attempts": int,
    "max_in_flight": int,
    "strict": bool,
    "scheduler": str,
    "name": str,
}

#: Recognized top-level keys. Anything else is a typo.
TOP_LEVEL = {
    "name", "description", "params", "schema", "pipeline", "width",
    "max_attempts", "max_in_flight", "strict", "workers", "scheduler",
    "on_complete", "logging", "paths",
}

LOG_LEVELS = ("error", "warning", "info", "debug", "trace")

#: Placeholder pattern for path templates.
_TEMPLATE = re.compile(r"\{([a-z_]+)\.([a-z_0-9]+)\}|\{(date|time|user)\}")


@dataclass
class RunConfig:
    """Every setting for one run, after merging and defaulting."""

    name: str
    params: str
    schema_source: Any
    pipeline_source: Any = None
    description: str = ""
    width: int = 1
    workers: int = 0                    # 0 means the CPU count
    max_attempts: int = 0               # 0 means no cap
    max_in_flight: int = 0              # 0 means no ceiling
    strict: bool = False
    scheduler: str = "pbs"
    on_complete: str = ""
    terminal_level: str = "info"
    file_level: str = "debug"
    log_file_name: str = "jobchain.log"
    work_dir_template: str = "{run.home}/work/{row.name}"
    log_dir_template: str = "{run.home}/logs"

    #: Absolute path of the configuration file, or "" when built directly.
    source_path: str = ""
    #: Verbatim text of the configuration as supplied.
    source_text: str = ""
    #: Where each non-default value came from, for the captured config.
    provenance: Dict[str, str] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("the run configuration must set 'name'")
        if not _RUN_NAME_RE.fullmatch(self.name):
            raise ConfigError(
                f"run name '{self.name}' may contain only letters, digits, "
                f"dots, dashes, and underscores"
            )
        if self.width < 1:
            raise ConfigError(f"width must be at least 1, got {self.width}")
        if self.workers < 0:
            raise ConfigError("workers cannot be negative")
        if self.scheduler not in ("pbs", "slurm"):
            raise ConfigError(
                f"scheduler must be 'pbs' or 'slurm', got '{self.scheduler}'"
            )
        for level, label in ((self.terminal_level, "logging.terminal"),
                             (self.file_level, "logging.file")):
            if level not in LOG_LEVELS:
                raise ConfigError(
                    f"{label} must be one of {', '.join(LOG_LEVELS)}, got '{level}'"
                )

    @property
    def base_dir(self) -> str:
        """Directory that relative paths in this configuration resolve against."""
        if self.source_path:
            return os.path.dirname(os.path.abspath(self.source_path))
        return os.getcwd()

    @property
    def params_path(self) -> str:
        """Absolute path of the parameter file."""
        return _resolve(self.params, self.base_dir)

    @property
    def effective_workers(self) -> int:
        return self.workers or (os.cpu_count() or 4)

    def home(self, root: Optional[str] = None) -> str:
        """State directory for this run."""
        base = root or os.path.join(os.path.dirname(self.params_path), ".jobchain")
        return os.path.join(base, self.name)

    def as_dict(self) -> Dict[str, Any]:
        """Render the effective configuration as plain data."""
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params_path,
            "width": self.width,
            "workers": self.effective_workers,
            "max_attempts": self.max_attempts,
            "max_in_flight": self.max_in_flight,
            "strict": self.strict,
            "scheduler": self.scheduler,
            "on_complete": self.on_complete,
            "logging": {"terminal": self.terminal_level,
                        "file": self.file_level,
                        "file_name": self.log_file_name},
            "paths": {"work_dir": self.work_dir_template,
                      "log_dir": self.log_dir_template},
        }


_RUN_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_config(path: str, overrides: Optional[Dict[str, Any]] = None) -> RunConfig:
    """Read a run configuration and apply command-line overrides.

    Overrides win over the file, which wins over the built-in defaults. Where
    each effective value came from is recorded, so the captured configuration
    can say so.
    """
    if not os.path.isfile(path):
        raise ConfigError(f"configuration file not found: {path}")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on site packages
        raise ConfigError("PyYAML is required to read configuration files") from exc

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must be a YAML mapping at the top level")

    unknown = set(document) - TOP_LEVEL
    if unknown:
        raise ConfigError(
            f"unknown key(s) {sorted(unknown)} in {path}; recognized keys are "
            f"{sorted(TOP_LEVEL)}"
        )

    for required in ("name", "params", "schema"):
        if required not in document:
            raise ConfigError(f"{path} is missing '{required}'")

    logging_section = document.get("logging") or {}
    paths_section = document.get("paths") or {}
    _reject_unknown(logging_section, {"terminal", "file", "file_name"}, "logging")
    _reject_unknown(paths_section, {"work_dir", "log_dir"}, "paths")

    provenance: Dict[str, str] = {}
    settings: Dict[str, Any] = {}

    def take(key: str, default: Any, caster=None) -> Any:
        if key in document:
            provenance[key] = "config"
            value = document[key]
            return caster(value) if caster else value
        provenance[key] = "default"
        return default

    settings["name"] = expand_run_name(str(document["name"]))
    settings["params"] = str(document["params"])
    settings["schema_source"] = document["schema"]
    settings["pipeline_source"] = document.get("pipeline")
    settings["description"] = str(document.get("description") or "")
    settings["width"] = take("width", 1, int)
    settings["workers"] = take("workers", 0, int)
    settings["max_attempts"] = take("max_attempts", 0, int)
    settings["max_in_flight"] = take("max_in_flight", 0, int)
    settings["strict"] = bool(take("strict", False))
    settings["scheduler"] = str(take("scheduler", "pbs")).lower()
    settings["on_complete"] = str(document.get("on_complete") or "")
    settings["terminal_level"] = str(logging_section.get("terminal", "info")).lower()
    settings["file_level"] = str(logging_section.get("file", "debug")).lower()
    settings["log_file_name"] = str(logging_section.get("file_name", "jobchain.log"))
    settings["work_dir_template"] = str(
        paths_section.get("work_dir", "{run.home}/work/{row.name}"))
    settings["log_dir_template"] = str(
        paths_section.get("log_dir", "{run.home}/logs"))

    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key == "run_name":
            settings["name"] = expand_run_name(str(value))
            provenance["name"] = "cli"
            continue
        if key not in OVERRIDABLE:
            raise UsageError(f"'{key}' cannot be overridden on the command line")
        settings[key] = OVERRIDABLE[key](value)
        provenance[key] = "cli"

    config = RunConfig(source_path=os.path.abspath(path), source_text=text,
                       provenance=provenance, **settings)
    return config


def expand_run_name(name: str) -> str:
    """Expand date, time, and user placeholders in a run name."""
    now = time.localtime()
    return (name.replace("{date}", time.strftime("%Y-%m-%d", now))
                .replace("{time}", time.strftime("%H%M%S", now))
                .replace("{user}", _username()))


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - depends on the environment
        return os.environ.get("USER", "unknown")


def _reject_unknown(mapping: Dict[str, Any], allowed: set, where: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigError(
            f"unknown key(s) {sorted(unknown)} in {where}; recognized keys are "
            f"{sorted(allowed)}"
        )


def _resolve(path: str, base_dir: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(path))
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(base_dir, expanded))


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def expand_template(template: str, run_name: str, run_home: str,
                    row: Optional[Dict[str, Any]] = None,
                    row_name: str = "", row_index: int = 0,
                    generation: int = 1, shell: bool = False) -> str:
    """Expand a path template against a run and, optionally, a row.

    Unknown placeholders are an error rather than being left in place: a path
    containing a literal brace almost always means a typo, and discovering
    that when a job fails to write is far worse than discovering it now.

    ``shell=True`` is for a template that becomes shell text (a `command:`
    stage), not a path: a ``{row.<column>}`` reference then expands to a
    ``$JC_<column>`` shell-variable reference instead of the value itself.
    The value reaches the script only through the row's already-quoted
    ``env`` file (see ``store.render_env``), sourced before the command
    runs, so row data can never inject shell syntax merely by being
    substituted into a template -- the shell parses command structure
    before it expands a variable, so nothing embedded in the value can
    introduce a new command, regardless of quoting in the template.
    """
    row = row or {}

    def replace(match: "re.Match") -> str:
        namespace, key, bare = match.group(1), match.group(2), match.group(3)
        if bare:
            now = time.localtime()
            if bare == "date":
                return time.strftime("%Y-%m-%d", now)
            if bare == "time":
                return time.strftime("%H%M%S", now)
            return _username()
        if namespace == "run":
            if key == "name":
                return run_name
            if key == "home":
                return run_home
            raise ConfigError(f"unknown template placeholder {{run.{key}}}")
        if namespace == "row":
            if key == "name":
                return row_name
            if key == "index":
                return str(row_index)
            if key == "generation":
                return str(generation)
            if key in row:
                return f"$JC_{key}" if shell else str(row[key])
            raise ConfigError(
                f"template placeholder {{row.{key}}} names no column; "
                f"available columns are {sorted(row)}"
            )
        raise ConfigError(f"unknown template namespace '{namespace}'")

    return _TEMPLATE.sub(replace, template)


def template_is_generation_aware(template: str) -> bool:
    """Whether a template namespaces output per attempt.

    When it does, re-running a row writes somewhere that does not yet exist,
    so nothing can be overwritten and the rerun confirmation is unnecessary.
    """
    return "{row.generation}" in template


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def render_final_config(config: RunConfig, schema_document: Any,
                        pipeline_document: Any) -> str:
    """Render the effective configuration as a complete, runnable document.

    Schema and pipeline are inlined and paths made absolute, so the result
    describes the run without reference to anything else. A comment on each
    non-default value records where it came from.
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ConfigError(
            "PyYAML is required to write the captured configuration") from exc

    payload = config.as_dict()
    payload["schema"] = schema_document
    if pipeline_document is not None:
        payload["pipeline"] = pipeline_document

    body = yaml.safe_dump(payload, default_flow_style=False, sort_keys=True)
    annotated: List[str] = [
        f"# Effective configuration for run '{config.name}',",
        f"# written by jobchain {VERSION}. This document is complete and",
        "# runnable: 'jobchain run config.final.yaml' reproduces this run.",
        "",
    ]
    for line in body.splitlines():
        key = line.split(":", 1)[0].strip()
        origin = config.provenance.get(key)
        if origin and origin != "default" and not line.startswith(" "):
            annotated.append(f"{line}    # from the {origin}")
        else:
            annotated.append(line)
    return "\n".join(annotated) + "\n"


def describe_settings(config: RunConfig) -> List[Tuple[str, str]]:
    """Render the settings a startup summary should show."""
    return [
        ("config", config.source_path or "(built directly)"),
        ("params", config.params_path),
        ("width", str(config.width)),
        ("workers", str(config.effective_workers)),
        ("scheduler", config.scheduler),
        ("strict", str(config.strict).lower()),
        ("max attempts", str(config.max_attempts) if config.max_attempts else "unlimited"),
    ]
