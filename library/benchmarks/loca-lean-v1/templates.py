"""Shared LOCA oracle, NOP, and mutation templates used by CI and tests."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Callable


def _rate(views: int, clicks: int) -> str:
    return f"{(views / clicks * 100 if clicks else 0.0):.3f}%"


def _rows(task_dir: Path) -> list[dict[str, str]]:
    with (task_dir / "files" / "clickstream.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def oracle_bytes(task_dir: Path) -> bytes:
    """Compute the expected final record solely from agent-visible state."""
    manifest = json.loads((task_dir / "state_manifest.json").read_text(encoding="utf-8"))
    rows = _rows(task_dir)
    rows_per_scenario = manifest["params"]["num_days"] * 24
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["scenario", "A_conversion %", "B_conversion %"])
    total = [0, 0, 0, 0]
    for start in range(0, len(rows), rows_per_scenario):
        group = rows[start : start + rows_per_scenario]
        if not group:
            continue
        values = [
            sum(int(row[key]) for row in group)
            for key in ("A_clicks", "A_store_views", "B_clicks", "B_store_views")
        ]
        total = [left + right for left, right in zip(total, values)]
        writer.writerow([group[0]["scenario"], _rate(values[1], values[0]), _rate(values[3], values[2])])
    writer.writerow([
        "overall (total_store_views/total_clicks)",
        _rate(total[1], total[0]),
        _rate(total[3], total[2]),
    ])
    return output.getvalue().encode("utf-8")


def oracle(task_dir: Path, workspace: Path | None = None) -> Path:
    workspace = (workspace or task_dir / "agent_workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    destination = workspace / "record.csv"
    destination.write_bytes(oracle_bytes(task_dir))
    expected = list(csv.reader(io.StringIO(destination.read_text(encoding="utf-8"))))
    if len(expected) > 1 and float(expected[-1][2].rstrip("%")) > float(expected[-1][1].rstrip("%")):
        (workspace / "promo-assets-for-b.marker").write_text("created by oracle\n", encoding="utf-8")
    return destination


def nop(_task_dir: Path, workspace: Path) -> None:
    """A deliberate no-op candidate; verifier must reject it."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "record.csv").write_text("scenario,A_conversion %,B_conversion %\n", encoding="utf-8")


def _mutate_swap_arms(task_dir: Path, workspace: Path) -> None:
    rows = list(csv.reader(io.StringIO(oracle_bytes(task_dir).decode())))
    for row in rows[1:]:
        row[1], row[2] = row[2], row[1]
    workspace.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    (workspace / "record.csv").write_text(output.getvalue(), encoding="utf-8")


def _mutate_drop_last(task_dir: Path, workspace: Path) -> None:
    rows = list(csv.reader(io.StringIO(oracle_bytes(task_dir).decode())))
    workspace.mkdir(parents=True, exist_ok=True)
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows[:-1])
    (workspace / "record.csv").write_text(output.getvalue(), encoding="utf-8")


def mutants() -> dict[str, Callable[[Path, Path], None]]:
    return {"swap-arms": _mutate_swap_arms, "drop-overall": _mutate_drop_last}
