"""Private start barrier. Persist process identity before executing project code."""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        return 64
    if sys.stdin.buffer.readline(16) != b"RUN\n":
        return 77
    argv = sys.argv[2:]
    os.execvpe(argv[0], argv, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
