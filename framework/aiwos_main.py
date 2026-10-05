"""Entry point used by the .ai/bin launchers (avoids PYTHONPATH quirks across shells)."""
import os
import sys

sys.dont_write_bytecode = True  # never leave __pycache__ in .ai/runtime (it would show up in work-package diffs)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiwos.cli import main  # noqa: E402

sys.exit(main())
