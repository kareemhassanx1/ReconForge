"""Allow ``python -m reconforge`` to run the command-line interface."""

from reconforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
