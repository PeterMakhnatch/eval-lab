"""Render anatomy cards for the 800 sampled cases."""
from __future__ import annotations

import json
import time
from pathlib import Path

from evallab.trace_anatomy import (
    build_anatomy,
    load_atif_file,
    load_run,
    render_anatomy_card,
)

COHORT_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/R1-PREVALENCE/receipts/cohort.jsonl")
CARDS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R1/cards")


def main() -> None:
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    cohort = [json.loads(line) for line in COHORT_PATH.read_text().splitlines() if line.strip()]
    repo_root = Path(".").resolve()

    print(f"Rendering cards for {len(cohort)} cases...")
    t0 = time.time()
    for idx, case in enumerate(cohort, 1):
        sid = case["sample_id"]
        out_path = CARDS_DIR / f"{sid}.md"
        if out_path.exists() and out_path.stat().st_size > 0:
            continue

        loader = case["loader"]
        if loader == "cloud":
            lr = load_run(case["source_id"], repo_root=repo_root)
        elif loader == "hacktrace_file":
            lr = load_atif_file(Path(case["file_path"]))
        else:
            raise ValueError(f"Unknown loader: {loader}")

        anat = build_anatomy(lr)
        card_text = render_anatomy_card(anat)
        out_path.write_text(card_text, encoding="utf-8")

        if idx % 100 == 0 or idx == len(cohort):
            elapsed = time.time() - t0
            print(f"Rendered {idx}/{len(cohort)} cards ({elapsed:.1f}s)")


if __name__ == "__main__":
    main()
