from __future__ import annotations

import sys

sys.dont_write_bytecode = True

from worldwatch.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
