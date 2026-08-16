"""Allow the package to be run with 'python3 -m jobchain'."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
