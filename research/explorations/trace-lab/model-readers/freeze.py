"""Freeze the model-reader prompts and schema (blindness gate).

Writes ``PROMPTS.sha256`` (sha256 of the exact Scout question, Docent
prompt, canonical Docent schema JSON, and both model strings) and
``FROZEN_AT`` (UTC timestamp). Run ONCE, after HAR-81/HAR-104 iteration
and BEFORE either reader touches the 12 HAR-119 runs. The frozen strings
live in ``prompts.py``; any later edit changes the digests.

Usage: uv run python research/explorations/trace-lab/model-readers/freeze.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from prompts import (  # noqa: E402
    DOCENT_MODEL,
    DOCENT_PROMPT,
    DOCENT_SCHEMA,
    SCOUT_MODEL,
    SCOUT_QUESTION,
)


def main() -> int:
    if (HERE / "FROZEN_AT").exists():
        print("already frozen; refusing to re-freeze", file=sys.stderr)
        return 2
    parts = {
        "scout_question": SCOUT_QUESTION,
        "docent_prompt": DOCENT_PROMPT,
        "docent_schema": json.dumps(DOCENT_SCHEMA, sort_keys=True),
        "scout_model": SCOUT_MODEL,
        "docent_model": DOCENT_MODEL,
    }
    lines = [f"{hashlib.sha256(v.encode()).hexdigest()}  {k}" for k, v in parts.items()]
    (HERE / "PROMPTS.sha256").write_text("\n".join(lines) + "\n")
    (HERE / "FROZEN_AT").write_text(
        datetime.now(UTC).isoformat(timespec="seconds") + "\n"
    )
    print("\n".join(lines))
    print("frozen at", (HERE / "FROZEN_AT").read_text().strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
