"""Stub scripts for testing repomap generator and checks."""
from __future__ import annotations

import sys
from pathlib import Path


def repomap_generator_main() -> None:
    """Simulates python -m evallab.repomap generate by rewriting docs/repo-map.md."""
    cwd = Path.cwd()
    docs_dir = cwd / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    repo_map = docs_dir / "repo-map.md"
    repo_map.write_text("# Repo Map\n- regenerated_symbol_after_rebase\n")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        repomap_generator_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "fail":
        sys.stderr.write("Simulated check failure\n")
        sys.exit(1)
    else:
        sys.exit(0)
