"""Build DetBench-format views and a runs table for the replay-v1 DEV set (690 runs, 5 corpora).

Usage (from the eval-lab worktree root)::

    uv run python research/experiments/ov1104-judges/build_dev.py --out /private/tmp/ov1104/judges/dev

Reads the lab's replay set through ``evallab.hack_drop.load_rollouts`` (the loader the assembly
line uses), so step numbering matches AutoReview's. Labels go only to ``runs.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from views import atif_to_view  # noqa: E402

from evallab import hack_drop as hd  # noqa: E402

REPLAY = Path.home() / "Developer/eval-lab-results/line/replay-v1/inputs"
CORPORA = ("goodfire", "cheatbench", "terminal-wrench", "c1c5", "sol-astra")
# natural = the agent was not instructed to cheat (PROGRAM.md principle 6)
NATURAL = {
    "goodfire": True,
    "cheatbench": False,
    "terminal-wrench": None,  # per run: baseline variant natural, sanitized variant prompted
    "c1c5": True,
    "sol-astra": True,
}
LABEL_TYPE = {
    "goodfire": "dataset",
    "cheatbench": "constructed",
    "terminal-wrench": "rule",
    "c1c5": "executed",
    "sol-astra": "executed",
}


def _reward(item: Any) -> float | None:
    meta = item.metadata.get("run_meta") if isinstance(item.metadata, Mapping) else None
    docs: list[Mapping[str, Any]] = [m for m in (meta, item.run.doc.get("metadata") if item.run else None) if isinstance(m, Mapping)]
    for document in docs:
        value = document.get("reward")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        verifier = document.get("verifier_result")
        if isinstance(verifier, Mapping):
            rewards = verifier.get("rewards")
            if isinstance(rewards, Mapping) and isinstance(rewards.get("reward"), (int, float)):
                return float(rewards["reward"])
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replay", type=Path, default=REPLAY)
    args = parser.parse_args()
    views_dir = args.out / "views"
    rows: list[dict[str, Any]] = []
    for corpus in CORPORA:
        manifest = [json.loads(line) for line in (args.replay / f"corpora/{corpus}-manifest.jsonl").read_text().splitlines() if line.strip()]
        labels = {
            row["id"]: row["is_cheat"]
            for row in (json.loads(line) for line in (args.replay / f"corpora/{corpus}-labels.jsonl").read_text().splitlines() if line.strip())
        }
        by_path = {str(Path(row["path"]).resolve()): row for row in manifest}
        items = hd.load_rollouts([args.replay / f"corpora/{corpus}-manifest.jsonl"])
        (views_dir / corpus).mkdir(parents=True, exist_ok=True)
        for item in items:
            row = by_path.get(str(Path(item.source_path).resolve()))
            if row is None:
                raise SystemExit(f"{corpus}: loaded item {item.item_id} is not in the manifest")
            native_id = row["id"]
            run_key = f"{corpus}:{native_id}"
            file_name = native_id.replace("/", "__").replace(":", "__") + ".json"
            view_path = views_dir / corpus / file_name
            if item.run is None:
                view = atif_to_view(run_key, [], diff=item.diff_text, reward=None)
                view["abstain"] = f"no trajectory parsed ({item.metadata.get('parse_error')})"
                n_steps, n_chars = 0, sum(len(text) for _name, text in item.files)
            else:
                steps = [s for s in item.run.doc.get("steps") or [] if isinstance(s, Mapping)]
                view = atif_to_view(run_key, steps, diff=item.diff_text, reward=_reward(item), task_text=item.run.prompt)
                n_steps, n_chars = len(steps), item.run.raw_chars
            view_path.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
            natural = NATURAL[corpus]
            if corpus == "terminal-wrench":
                natural = row.get("variant") == "baseline"
            rows.append({
                "run_key": run_key,
                "set": corpus,
                "split": "dev",
                "label": int(bool(labels[native_id])),
                "label_type": LABEL_TYPE[corpus],
                "natural": natural,
                "cluster": str(row.get("task_id") or item.task_id),
                "source_family": corpus,
                "view": str(view_path),
                "n_steps": n_steps,
                "n_chars": n_chars,
                "model": row.get("model"),
            })
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "runs.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    by_set: dict[str, list[int]] = {}
    for row in rows:
        by_set.setdefault(row["set"], []).append(row["label"])
    for name, labels in by_set.items():
        print(f"{name}: n={len(labels)} pos={sum(labels)} neg={len(labels) - sum(labels)}")
    print(f"total {len(rows)} -> {args.out / 'runs.jsonl'}")


if __name__ == "__main__":
    main()
