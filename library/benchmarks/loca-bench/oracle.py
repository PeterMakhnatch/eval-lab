"""Reference solution derived from the pinned upstream expected-ratio output."""
from __future__ import annotations
import shutil
from pathlib import Path

def solve(task_dir: Path, workspace: Path | None = None) -> Path:
    task_dir = task_dir.resolve(); workspace = (workspace or task_dir / "agent_workspace").resolve(); workspace.mkdir(parents=True, exist_ok=True)
    shutil.copy2(task_dir / "files" / "expected_record.csv", workspace / "record.csv")
    rows = (workspace / "record.csv").read_text(encoding="utf-8").splitlines()
    if rows and len(rows[-1].split(",")) >= 3:
        a, b = rows[-1].split(",")[1:3]
        if float(b.rstrip("%")) > float(a.rstrip("%")):
            (workspace / "promo-assets-for-b.marker").write_text("created by oracle\n", encoding="utf-8")
    return workspace / "record.csv"

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(); p.add_argument("--task-dir", type=Path, required=True); p.add_argument("--workspace", type=Path); args = p.parse_args(); print(solve(args.task_dir, args.workspace))
