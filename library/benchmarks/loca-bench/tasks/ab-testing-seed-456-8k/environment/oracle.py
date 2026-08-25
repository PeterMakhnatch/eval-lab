"""Reference solution that computes the record from the agent-visible clickstream."""
from __future__ import annotations
import csv, json
from pathlib import Path


def _rate(clicks: int, views: int) -> float:
    return views / clicks if clicks > 0 else 0.0


def solve(task_dir: Path, workspace: Path | None = None) -> Path:
    """Compute record.csv from the agent-visible clickstream CSV.

    The reference solution never reads a golden/expected file.  It uses the
    scenario count and rows-per-scenario from ``state_manifest.json`` to split
    the flattened clickstream into the original scenarios, so duplicate
    scenario names are still handled correctly.
    """
    task_dir = task_dir.resolve()
    workspace = (workspace or task_dir / "agent_workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    manifest = json.loads((task_dir / "state_manifest.json").read_text(encoding="utf-8"))
    params = manifest["params"]
    num_days = params["num_days"]
    rows_per_scenario = num_days * 24

    clickstream = task_dir / "files" / "clickstream.csv"
    with clickstream.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    record_path = workspace / "record.csv"
    with record_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["scenario", "A_conversion %", "B_conversion %"])

        total_a_clicks = total_a_views = 0
        total_b_clicks = total_b_views = 0
        for i in range(0, len(rows), rows_per_scenario):
            group = rows[i : i + rows_per_scenario]
            if not group:
                break
            name = group[0]["scenario"]
            a_clicks = sum(int(r["A_clicks"]) for r in group)
            a_views = sum(int(r["A_store_views"]) for r in group)
            b_clicks = sum(int(r["B_clicks"]) for r in group)
            b_views = sum(int(r["B_store_views"]) for r in group)
            total_a_clicks += a_clicks
            total_a_views += a_views
            total_b_clicks += b_clicks
            total_b_views += b_views
            writer.writerow(
                [
                    name,
                    f"{_rate(a_clicks, a_views) * 100:.3f}%",
                    f"{_rate(b_clicks, b_views) * 100:.3f}%",
                ]
            )

        overall_a = _rate(total_a_clicks, total_a_views)
        overall_b = _rate(total_b_clicks, total_b_views)
        writer.writerow(
            [
                "overall (total_store_views/total_clicks)",
                f"{overall_a * 100:.3f}%",
                f"{overall_b * 100:.3f}%",
            ]
        )

    if overall_b > overall_a:
        (workspace / "promo-assets-for-b.marker").write_text(
            "created by oracle\n", encoding="utf-8"
        )
    return record_path


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--task-dir", type=Path, required=True)
    p.add_argument("--workspace", type=Path)
    args = p.parse_args()
    print(solve(args.task_dir, args.workspace))
