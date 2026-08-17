"""The entry point: dispatch, and where an exception becomes an exit code.

This is the single place a traceback reaching the terminal always indicates a
defect in this tool rather than bad input.
"""

from __future__ import annotations

import traceback
from typing import Optional, Sequence

from ..core import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    VERSION,
    JobChainError,
    configure_logging,
    get_logger,
)
from .commands import _HANDLERS
from .parser import build_parser
from .support import PROGRAM

# Named entry.py rather than main.py: a module with the same name as its own
# exported main() function would shadow the submodule attribute on the
# package once __init__.py does `from .entry import main`, which breaks
# mock.patch("jobchain.cli.main.X")-style targeting of this module's own
# imports (the same collision that made operations/{prepare,doctor,cancel}.py
# become lifecycle.py/reconcile.py/cancellation.py).


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse arguments, dispatch, and translate failures into exit codes."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    configure_logging(verbosity=getattr(args, "verbose", 0))

    handler = _HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.error(f"unknown command {args.command}")
        return EXIT_USAGE

    try:
        return handler(args)
    except JobChainError as exc:
        get_logger().error("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        get_logger().error("interrupted")
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover - depends on the consumer
        return EXIT_OK
    except Exception as exc:
        # Reaching here means an unexpected condition escaped the layers
        # below, which is a defect in this tool rather than bad input.
        get_logger().error(
            "internal error: %s. This is a defect in %s %s, not a problem with "
            "the input; the traceback below belongs in a bug report.",
            exc, PROGRAM, VERSION)
        traceback.print_exc()
        return EXIT_INTERNAL
