"""Allows ``python -m research_desk …`` as well as the installed console script."""

from research_desk.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
