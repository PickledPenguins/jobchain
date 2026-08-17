"""jobchain - scheduler job pipelines driven by a delimited parameter file.

The public surface is re-exported here. Stage classes and validation classes
written by users import from this package, so everything they need is
reachable without knowing the module layout.
"""

from __future__ import annotations

from .core import (
    VERSION,
    ConfigError,
    ConflictError,
    DataError,
    JobChainError,
    NodeHelperError,
    PipelineError,
    SchedulerError,
    SchemaError,
    StateError,
    StructureError,
    UsageError,
)
from .parse import ScanReport, scan
from .pipeline import (
    Choice,
    Integer,
    JobStage,
    Pipeline,
    Setting,
    StageBool,
    Text,
    load_pipeline_source,
)
from .schema import (
    AllOf,
    AnyOf,
    Bool,
    Exact,
    Field,
    Float,
    Int,
    OneOf,
    OutputPath,
    PathExists,
    Regex,
    Schema,
    SchemaBase,
    Str,
    Validator,
    load_schema,
    load_schema_source,
)

__version__ = VERSION

__all__ = [
    "VERSION",
    "__version__",
    # Exceptions, each carrying the exit code the front end reports.
    "JobChainError",
    "UsageError",
    "ConfigError",
    "SchemaError",
    "PipelineError",
    "DataError",
    "StructureError",
    "StateError",
    "SchedulerError",
    "NodeHelperError",
    "ConflictError",
    # Writing a schema.
    "Validator",
    "Int",
    "Float",
    "Str",
    "Bool",
    "OneOf",
    "Exact",
    "Regex",
    "PathExists",
    "OutputPath",
    "AllOf",
    "AnyOf",
    "Field",
    "Schema",
    "SchemaBase",
    "load_schema",
    "load_schema_source",
    # Writing a pipeline stage.
    "JobStage",
    "Pipeline",
    "Setting",
    "Choice",
    "StageBool",
    "Integer",
    "Text",
    "load_pipeline_source",
    # Reading a parameter file.
    "ScanReport",
    "scan",
]
