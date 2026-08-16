"""Foundation layer: exit codes, the exception hierarchy, and logging.

This module is the bottom of the dependency graph. It may not import any
other jobchain module.

Every failure that reaches the command line is expected to arrive as a
JobChainError subclass, which carries the exit code the front end should
use. A traceback reaching the terminal always indicates a defect in this
tool, never bad input; the front end enforces that distinction.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

#: Semantic version of the whole project (Python package and C helper).
VERSION = "0.5-v4b"

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
#
# Each class of failure gets a distinct code so that wrapper scripts can
# branch on the cause without parsing message text.

EXIT_OK = 0
EXIT_USAGE = 1           # bad command line
EXIT_INTERNAL = 2        # defect in this tool
EXIT_DATA = 3            # the parameter file failed validation
EXIT_STRUCTURE = 4       # the parameter file could not be parsed at all
EXIT_CONFIG = 5          # the configuration, schema, or pipeline is invalid
EXIT_SCHEMA = EXIT_CONFIG  # retained name; schema errors share the code
EXIT_STATE = 6           # the run directory is missing or inconsistent
EXIT_SCHEDULER = 7       # qsub/sbatch missing, or a submission was rejected
EXIT_NODE_HELPER = 8     # the C helper is missing or failed
EXIT_CONFLICT = 9        # the requested edit conflicts with a running job

EXIT_NAMES = {
    EXIT_OK: "ok",
    EXIT_USAGE: "usage",
    EXIT_INTERNAL: "internal error",
    EXIT_DATA: "data validation failure",
    EXIT_STRUCTURE: "structural failure",
    EXIT_CONFIG: "configuration error",
    EXIT_STATE: "state error",
    EXIT_SCHEDULER: "scheduler error",
    EXIT_NODE_HELPER: "node helper error",
    EXIT_CONFLICT: "conflict with a running job",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JobChainError(Exception):
    """Base class for every expected failure.

    ``exit_code`` is what the front end returns to the shell.

    Failures carry a message and nothing else. Suggested follow-up commands
    are deliberately absent: hint text spread across dozens of messages is a
    maintenance burden and drifts out of step with the commands it names.
    Commands are documented once, in the command reference.
    """

    exit_code = EXIT_INTERNAL

    def __init__(self, message: str):
        super().__init__(message)


class UsageError(JobChainError):
    """The command line was well-formed but asks for something impossible."""

    exit_code = EXIT_USAGE


class ConfigError(JobChainError):
    """The run configuration, schema, or pipeline is invalid."""

    exit_code = EXIT_CONFIG


class SchemaError(ConfigError):
    """The schema is missing, malformed, or semantically invalid."""


class PipelineError(ConfigError):
    """The pipeline definition is invalid, or a stage class cannot be used."""


class DataError(JobChainError):
    """One or more rows of the parameter file failed validation."""

    exit_code = EXIT_DATA


class StructureError(JobChainError):
    """The parameter file could not be parsed into rows and fields."""

    exit_code = EXIT_STRUCTURE


class StateError(JobChainError):
    """The run directory is absent, incomplete, or internally inconsistent."""

    exit_code = EXIT_STATE


class SchedulerError(JobChainError):
    """A scheduler binary is unavailable or rejected a submission."""

    exit_code = EXIT_SCHEDULER


class NodeHelperError(JobChainError):
    """The compiled helper is missing, unusable, or returned a failure."""

    exit_code = EXIT_NODE_HELPER


class ConflictError(JobChainError):
    """An edit was requested against a row whose job is still active."""

    exit_code = EXIT_CONFLICT


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

#: Level below DEBUG, used for per-file and per-syscall detail.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

LOGGER_NAME = "jobchain"


def trace(message: str, *args: object) -> None:
    """Emit a TRACE-level message on the shared jobchain logger."""
    logging.getLogger(LOGGER_NAME).log(TRACE, message, *args)


def get_logger() -> logging.Logger:
    """Return the shared jobchain logger.

    All modules log through this single logger so that one verbosity setting
    from the command line governs the entire run.
    """
    return logging.getLogger(LOGGER_NAME)


#: Names accepted for a logging level, mapped to their numeric value.
LEVEL_NAMES = {"error": logging.ERROR, "warning": logging.WARNING,
               "info": logging.INFO, "debug": logging.DEBUG, "trace": TRACE}


def configure_logging(verbosity: int = 0, log_file: Optional[str] = None,
                      use_color: Optional[bool] = None,
                      terminal_level: Optional[str] = None,
                      file_level: Optional[str] = None) -> logging.Logger:
    """Configure the shared logger from the front end's verbosity flags.

    The console shows informational progress by default; -v adds debug detail
    and -vv adds trace. The run's log file records at its own level, normally
    higher than the console's, so a quiet run still leaves a full record.
    Handlers are replaced rather than appended, so repeated calls do not
    duplicate output.
    """
    if terminal_level:
        level = LEVEL_NAMES.get(terminal_level.lower(), logging.INFO)
    else:
        level = {0: logging.INFO, 1: logging.DEBUG}.get(verbosity, TRACE)
    # An explicit -v always wins over a configured level, so a person asking
    # for detail on the command line gets it.
    if verbosity:
        level = min(level, {1: logging.DEBUG}.get(verbosity, TRACE))
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if use_color is None:
        use_color = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_Formatter(use_color))
    logger.addHandler(stream)

    if log_file:
        # The file handler always records full detail regardless of the
        # console verbosity, so a quiet run still leaves a usable log.
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(LEVEL_NAMES.get((file_level or "debug").lower(),
                                              logging.DEBUG))
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        logger.addHandler(file_handler)
        # The logger must pass everything either sink wants to see.
        logger.setLevel(min(level, file_handler.level))

    return logger


_COLORS = {
    "TRACE": "\033[90m",
    "DEBUG": "\033[36m",
    "INFO": "\033[0m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}


class _Formatter(logging.Formatter):
    """Console formatter that prefixes non-informational levels with a tag."""

    def __init__(self, use_color: bool):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno == logging.INFO:
            text = message
        else:
            text = f"{record.levelname.lower()}: {message}"
        if self.use_color:
            return f"{_COLORS.get(record.levelname, '')}{text}\033[0m"
        return text


def log_startup_summary(title: str, settings: dict) -> None:
    """Log the effective settings for a command before it does any work.

    Every applied default and resolved path is recorded here, so that a
    verbose log always answers "what did it actually think it was doing"
    without requiring the run to be repeated.
    """
    logger = get_logger()
    logger.debug("%s", title)
    for key in sorted(settings):
        logger.debug("  %-22s %s", key, settings[key])
