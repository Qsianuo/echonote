"""PyInstaller entry point for the portable Windows build.

PyInstaller runs the entry script as ``__main__``, so the real code cannot use
relative imports here. Importing the package first keeps ``app.desktop`` a
normal submodule and lets ``python -m app`` stay the developer path.
"""

from __future__ import annotations

import sys

from app.desktop import main

if __name__ == "__main__":
    sys.exit(main())
