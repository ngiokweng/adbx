"""Run the adbx command line interface with python -m adbx."""

# PyInstaller executes this file as a script without a parent package.
from adbx.cli import main

raise SystemExit(main())
