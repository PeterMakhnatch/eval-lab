"""Skip profiling only for a proven documentation-only diff; unknown means run."""

from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath


def _documentation_only(path: str) -> bool:
    return path in {"AGENTS.md", "README.md"} or (
        PurePosixPath(path).suffix == ".md"
        and path.startswith(("docs/", "agents/", ".omp/skills/", ".claude/skills/"))
    )


def main() -> None:
    base, head = sys.argv[1:]
    if not base or not head or set(base) == {"0"}:
        print("profile=true")
        return
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"],
        capture_output=True,
        check=False,
    )
    paths = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")[:-1]
    skip = result.returncode == 0 and bool(paths) and all(map(_documentation_only, paths))
    print(f"profile={'false' if skip else 'true'}")
    if result.returncode:
        print("Change inventory unavailable; retaining full profiling.", file=sys.stderr)


if __name__ == "__main__":
    main()
