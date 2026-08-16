"""Pipelines: stage definitions, the JobStage interface, and class resolution.

A pipeline is an ordered series of stages. Each stage becomes one submit
script per valid row, and the scheduler chains them with dependency arguments
supplied at submission time. Nothing inside a script refers to another job, so
every script stays independently resubmittable.

A stage's ``name`` is a label. Its implementing class comes from ``uses``,
defaulting to the name with each underscore-separated word capitalized. That
keeps names free: two stages may share a class with different configuration,
and renaming a stage never breaks code.

Stage instances are frozen after construction. A class physically cannot cache
into ``self``, which is what makes generating scripts across a thread pool
safe without asking stage authors to think about it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence

from .core import PipelineError, get_logger, trace
from .schema import _import_module

#: Dependency types a stage may declare against the stage before it.
DEPENDS_TYPES = ("afterok", "afterany", "afternotok")

#: Resource keys understood by the script renderer.
RESOURCE_KEYS = ("walltime", "nodes", "ncpus", "ngpus", "mem", "queue", "account")

#: Keys a stage block may hold beyond its class's declared settings.
STAGE_KEYS = {"name", "uses", "depends", "chains_next", "command",
              "extra_directives", "env", *RESOURCE_KEYS}


# ---------------------------------------------------------------------------
# Setting declarations
# ---------------------------------------------------------------------------


class Setting:
    """A configuration key a stage class accepts.

    Declaring settings lets a typo in the pipeline YAML be caught at load
    time, with the same clarity as a bad parameter row, rather than surfacing
    as a missing key while a script is being written.
    """

    def __init__(self, default: Any = None, required: bool = False):
        self.default = default
        self.required = required

    def check(self, name: str, value: Any) -> Any:
        return value

    def describe(self) -> str:
        return "any value"


class Choice(Setting):
    """One of a fixed set of values."""

    def __init__(self, values: Sequence[Any], default: Any = None,
                 required: bool = False):
        super().__init__(default, required)
        self.values = list(values)

    def check(self, name: str, value: Any) -> Any:
        if value not in self.values:
            raise PipelineError(
                f"setting '{name}' must be one of "
                f"{', '.join(str(v) for v in self.values)}, got {value!r}"
            )
        return value

    def describe(self) -> str:
        return "one of: " + ", ".join(str(v) for v in self.values)


class Bool(Setting):
    """A boolean setting."""

    def check(self, name: str, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        raise PipelineError(f"setting '{name}' must be true or false, got {value!r}")

    def describe(self) -> str:
        return "true or false"


class Integer(Setting):
    """An integer setting, with optional bounds."""

    def __init__(self, default: Any = None, min: Optional[int] = None,
                 max: Optional[int] = None, required: bool = False):
        super().__init__(default, required)
        self.min = min
        self.max = max

    def check(self, name: str, value: Any) -> Any:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise PipelineError(
                f"setting '{name}' must be an integer, got {value!r}") from None
        if self.min is not None and number < self.min:
            raise PipelineError(f"setting '{name}' must be at least {self.min}")
        if self.max is not None and number > self.max:
            raise PipelineError(f"setting '{name}' must be at most {self.max}")
        return number

    def describe(self) -> str:
        if self.min is not None and self.max is not None:
            return f"an integer between {self.min} and {self.max}"
        return "an integer"


class Text(Setting):
    """A string setting."""

    def check(self, name: str, value: Any) -> Any:
        return str(value)

    def describe(self) -> str:
        return "text"


# ---------------------------------------------------------------------------
# The stage interface
# ---------------------------------------------------------------------------


class JobStage:
    """One stage of a job pipeline.

    A single instance is created per stage and reused for every row, so it
    holds no per-row state. ``write_script`` is called once for each valid
    row, producing that row's script for this stage.

    Everything about the resulting script is this class's choice: its
    contents, its directives, its template, and the directory it is written
    to. jobchain supplies the row and the contexts, then records the path
    that is returned.
    """

    #: Configuration keys this stage accepts beyond the standard resource
    #: keys, as a mapping of name to Setting. Read only; a subclass replaces
    #: it wholesale rather than mutating it.
    settings: ClassVar[Dict[str, Setting]] = {}

    #: Set by __init__ through object.__setattr__, because instances are
    #: frozen immediately afterwards.
    name: str
    config: Mapping[str, Any]
    run: Any

    def __init__(self, name: str, config: Mapping[str, Any], run: Any):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "run", run)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, key: str, value: Any) -> None:
        # Instances are shared across every row and across worker threads.
        # Caching into self would make generation order-dependent, so it is
        # refused outright rather than documented as a hazard.
        if getattr(self, "_frozen", False):
            raise PipelineError(
                f"stage '{self.name}' tried to set self.{key}: JobStage "
                f"instances are frozen because one instance serves every row "
                f"and every worker thread. Use a class attribute for lookup "
                f"tables, or a local variable inside write_script."
            )
        object.__setattr__(self, key, value)

    # -- what a subclass overrides ---------------------------------------

    def resources(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Scheduler parameters for this row, merged over the YAML block.

        Returning an empty mapping, which is the default, uses the YAML
        values unchanged.
        """
        return {}

    def output_dir(self, row: Mapping[str, Any], ctx: Any) -> str:
        """Directory this stage's script is written to for this row.

        The default is the row's working directory, which jobchain has
        already expanded from the configured template. An implementation may
        instead use a column from the row, a value from the YAML, or a fixed
        path.
        """
        return ctx.work_dir

    def script_name(self, row: Mapping[str, Any]) -> str:
        """File name for this stage's script."""
        return f"{self.config['_position']:02d}-{self.name}.sh"

    def write_script(self, row: Mapping[str, Any], ctx: Any) -> str:
        """Write this row's script for this stage; return its absolute path."""
        command = self.config.get("command")
        if not command:
            raise PipelineError(
                f"stage '{self.name}' has no write_script implementation and "
                f"no 'command'"
            )
        body = ctx.expand(str(command), row)
        return ctx.write(
            "#!/bin/sh\n"
            f"{ctx.directives(self.effective_resources(row))}\n"
            f"{ctx.preamble()}\n"
            "\n"
            f"{body}\n"
            "rc=$?\n"
            "\n"
            f"{ctx.epilogue()}\n"
            "exit $rc\n"
        )

    # -- used by jobchain ------------------------------------------------

    def effective_resources(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Merge YAML resources with whatever the class returns for this row."""
        merged = {key: self.config.get(key) for key in RESOURCE_KEYS}
        override = self.resources(row) or {}
        unknown = set(override) - set(RESOURCE_KEYS)
        if unknown:
            raise PipelineError(
                f"stage '{self.name}' returned unknown resource key(s) "
                f"{sorted(unknown)}; valid keys are {list(RESOURCE_KEYS)}"
            )
        for key, value in override.items():
            if value is not None:
                merged[key] = value
        merged["extra_directives"] = list(self.config.get("extra_directives") or [])
        merged["env"] = dict(self.config.get("env") or {})
        return merged

    def __repr__(self) -> str:
        return f"<{type(self).__name__} stage {self.name!r}>"


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


@dataclass
class StageSpec:
    """One stage's declaration, before its class is constructed."""

    name: str
    position: int
    depends: str
    chains_next: bool
    config: Dict[str, Any]
    class_name: str
    #: Whether the pipeline stated a dependency, as opposed to taking the
    #: default. The chaining stage's default is promoted, an explicit value
    #: never is.
    depends_explicit: bool = False


@dataclass
class Pipeline:
    """An ordered series of stages, with their implementing classes."""

    name: str
    specs: List[StageSpec] = dc_field(default_factory=list)
    stage_module: Optional[str] = None
    description: str = ""
    version: Optional[str] = None
    source_path: Optional[str] = None
    document: Any = None
    stages: List[JobStage] = dc_field(default_factory=list)

    @property
    def stage_names(self) -> List[str]:
        return [spec.name for spec in self.specs]

    @property
    def chaining_stage(self) -> str:
        """The stage that claims the next row."""
        for spec in self.specs:
            if spec.chains_next:
                return spec.name
        return self.specs[-1].name

    def stage(self, name: str) -> JobStage:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise PipelineError(f"no stage named '{name}' in pipeline '{self.name}'")

    def spec(self, name: str) -> StageSpec:
        for spec in self.specs:
            if spec.name == name:
                return spec
        raise PipelineError(f"no stage named '{name}' in pipeline '{self.name}'")

    def construct(self, run_context: Any) -> None:
        """Build one instance of each stage's class."""
        module = None
        if self.stage_module:
            module = _import_module(self.stage_module,
                                    self.source_path or os.getcwd())
        self.stages = [_build_stage(spec, module, self.stage_module, run_context)
                       for spec in self.specs]


def single_job_pipeline(command: str = "") -> Pipeline:
    """The implicit pipeline for a configuration with no pipeline section.

    One stage, named 'job', which chains. This keeps every downstream code
    path identical whether or not a pipeline was configured.
    """
    spec = StageSpec(name="job", position=1, depends="afterok", chains_next=True,
                     config={"_position": 1, "command": command}, class_name="Job")
    return Pipeline(name="single", specs=[spec])


def load_pipeline_source(source: Any, base_dir: str) -> Pipeline:
    """Build a Pipeline from a mapping or a path."""
    if isinstance(source, dict):
        return _build_pipeline(source, os.path.join(base_dir, "<inline>"))
    if isinstance(source, str):
        path = source if os.path.isabs(source) else os.path.join(base_dir, source)
        if not os.path.isfile(path):
            raise PipelineError(f"pipeline file not found: {path}")
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise PipelineError("PyYAML is required to read pipeline files") from exc
        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        if not isinstance(document, dict):
            raise PipelineError(f"{path} must be a YAML mapping at the top level")
        return _build_pipeline(document, path)
    raise PipelineError(
        f"pipeline must be a mapping or a path, got {type(source).__name__}")


def _build_pipeline(document: Dict[str, Any], path: str) -> Pipeline:
    """Turn a parsed pipeline document into a Pipeline."""
    allowed = {"name", "version", "description", "stage_module", "defaults", "stages"}
    unknown = set(document) - allowed
    if unknown:
        raise PipelineError(
            f"unknown key(s) {sorted(unknown)} in pipeline; recognized keys are "
            f"{sorted(allowed)}"
        )

    entries = document.get("stages") or []
    if not entries:
        raise PipelineError("a pipeline must declare at least one stage")

    defaults = document.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise PipelineError("pipeline 'defaults' must be a mapping")

    specs: List[StageSpec] = []
    seen: Dict[str, int] = {}
    for position, entry in enumerate(entries, start=1):
        spec = _build_stage_spec(entry, position, defaults)
        if spec.name in seen:
            raise PipelineError(
                f"stage '{spec.name}' is declared twice, at positions "
                f"{seen[spec.name]} and {position}; stage names must be unique"
            )
        seen[spec.name] = position
        specs.append(spec)

    _resolve_chaining(specs)

    return Pipeline(
        name=str(document.get("name") or "pipeline"),
        version=_optional_str(document.get("version")),
        description=str(document.get("description") or ""),
        stage_module=_optional_str(document.get("stage_module")),
        specs=specs,
        source_path=os.path.abspath(path),
        document=document,
    )


def _build_stage_spec(entry: Any, position: int,
                      defaults: Dict[str, Any]) -> StageSpec:
    """Build one stage declaration, merging pipeline defaults beneath it."""
    if not isinstance(entry, dict):
        raise PipelineError(f"stage #{position} must be a mapping")
    name = entry.get("name")
    if not name:
        raise PipelineError(f"stage #{position} is missing a 'name'")
    name = str(name)

    depends_explicit = "depends" in entry
    depends = str(entry.get("depends", "afterok")).lower()
    if depends not in DEPENDS_TYPES:
        raise PipelineError(
            f"stage '{name}' has depends '{depends}'; valid values are "
            f"{', '.join(DEPENDS_TYPES)}"
        )

    config: Dict[str, Any] = dict(defaults)
    config.update({k: v for k, v in entry.items()
                   if k not in ("name", "depends", "chains_next")})
    config["_position"] = position

    return StageSpec(
        name=name,
        position=position,
        depends=depends,
        chains_next=bool(entry.get("chains_next", False)),
        config=config,
        class_name=str(entry.get("uses") or _class_name_for(name)),
        depends_explicit=depends_explicit,
    )


def _resolve_chaining(specs: List[StageSpec]) -> None:
    """Decide which stage claims the next row, and check it can.

    The default is the last stage. A chaining stage must depend 'afterany',
    or an upstream failure would cancel it and the chain would stop silently.
    """
    marked = [spec for spec in specs if spec.chains_next]
    if len(marked) > 1:
        raise PipelineError(
            f"stages {', '.join(s.name for s in marked)} all set chains_next; "
            f"exactly one stage claims the next row"
        )
    if not marked:
        specs[-1].chains_next = True
        marked = [specs[-1]]

    chaining = marked[0]
    if chaining.position == 1:
        return  # the first stage has no dependency, so the rule cannot apply

    if chaining.depends != "afterany":
        if chaining.depends_explicit:
            raise PipelineError(
                f"stage '{chaining.name}' chains the next row but depends "
                f"'{chaining.depends}'; a chaining stage must depend "
                f"'afterany', or the chain stops whenever an earlier stage "
                f"fails"
            )
        # The dependency was defaulted rather than chosen. Promoting it means
        # a pipeline that says nothing about chaining still survives a failed
        # stage, which is the behaviour anyone would want.
        chaining.depends = "afterany"
        get_logger().debug(
            "stage '%s' chains the next row, so its dependency is afterany",
            chaining.name)


def _class_name_for(stage_name: str) -> str:
    """Map a stage name to its default class name."""
    return "".join(part.capitalize() for part in stage_name.split("_") if part)


def _build_stage(spec: StageSpec, module: Any, module_path: Optional[str],
                 run_context: Any) -> JobStage:
    """Construct one stage's class, validating its declared settings."""
    cls: Optional[type] = None
    if module is not None:
        candidate = getattr(module, spec.class_name, None)
        if candidate is not None:
            if not (isinstance(candidate, type) and issubclass(candidate, JobStage)):
                raise PipelineError(
                    f"stage '{spec.name}' resolves to '{spec.class_name}' in "
                    f"{module_path}, which is not a JobStage subclass"
                )
            cls = candidate

    if cls is None:
        if not spec.config.get("command"):
            available = sorted(
                key for key, value in vars(module or object).items()
                if isinstance(value, type) and issubclass(value, JobStage)
                and value is not JobStage
            ) if module is not None else []
            raise PipelineError(
                f"stage '{spec.name}' has no class '{spec.class_name}' in "
                f"{module_path or '(no stage_module set)'} and no 'command'"
                + (f". Available classes: {', '.join(available)}" if available
                   else "")
            )
        cls = JobStage

    config = _validate_settings(spec, cls)
    trace("stage %s -> %s", spec.name, cls.__name__)
    return cls(spec.name, config, run_context)


def _validate_settings(spec: StageSpec, cls: type) -> Dict[str, Any]:
    """Check a stage's YAML block against its class's declared settings."""
    declared: Dict[str, Setting] = dict(getattr(cls, "settings", {}) or {})
    allowed = STAGE_KEYS | set(declared) | {"_position"}

    unknown = {key for key in spec.config if key not in allowed}
    if unknown:
        known = sorted(allowed - {"_position"})
        raise PipelineError(
            f"stage '{spec.name}' has unknown key(s) {sorted(unknown)}; "
            f"recognized keys are {known}"
        )

    config = dict(spec.config)
    for key, setting in declared.items():
        if key in config:
            config[key] = setting.check(key, config[key])
        elif setting.required:
            raise PipelineError(
                f"stage '{spec.name}' is missing required setting '{key}' "
                f"({setting.describe()})"
            )
        else:
            config[key] = setting.default
    return config


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def describe_pipeline(pipeline: Pipeline) -> List[str]:
    """Render a pipeline's stages for a startup summary."""
    lines = []
    for spec in pipeline.specs:
        marks = []
        if spec.position > 1:
            marks.append(spec.depends)
        if spec.chains_next:
            marks.append("chains next")
        lines.append(f"  {spec.name:<16} {spec.class_name:<16} "
                     + "  ".join(marks))
    return lines
