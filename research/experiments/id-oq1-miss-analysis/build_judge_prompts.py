"""ID-OQ-1: build frozen-scorer-style judge prompts that judge EVERY finding (not only gate-eligible ones).

Condition O uses the original scorer-only key text; condition E uses the enriched MiMo key text from enrich_keys.py.
The instruction paragraph is the retained OQ-6 v4 scorer prompt header, byte for byte. Findings are anonymised
(id, direction, mechanism, citations; no confidence, no eligibility). The task source is the audit prompt's
`TASK SOURCE:` section, without the retrieved-context block. DEV only; no model calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SCORER_REF = Path(
    "/Users/petermakhnatch/Developer/eval-lab-results/2026-10-27/oq6-tq-gate-rerun-v4/calls/"
    "anthropic--claude-sonnet-5-5/B/dev/D0009/scorer/prompt.txt"
)
KEY_MARK = "ADMITTED SCORER-ONLY KEY:"
SOURCE_MARK = "\nTASK SOURCE:\n"


def header() -> str:
    text = SCORER_REF.read_text()
    return text[: text.index(KEY_MARK)]


def source_section(prompt_path: str) -> str:
    text = Path(prompt_path).read_text()
    return text[text.index(SOURCE_MARK) :]


def render(bundle: dict, head: str) -> str:
    keys = [
        {
            "key_id": k["key_id"],
            "category": k.get("category"),
            "root_causes": k.get("root_causes"),
            "summary": k["summary"],
        }
        for k in bundle["keys"]
    ]
    findings = [
        {"id": f["id"], "direction": f["direction"], "mechanism": f["mechanism"], "citations": f["citations"]}
        for f in bundle["findings"]
    ]
    return (
        head
        + f"{KEY_MARK}\n{json.dumps(keys, ensure_ascii=False)}\n"
        + f"ANONYMOUS FINDINGS:\n{json.dumps(findings, ensure_ascii=False)}"
        + source_section(bundle["prompt_path"])
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--enriched", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    head = header()
    counts = {"O": 0, "E": 0}
    for path in sorted(args.original.glob("D*.json")):
        original = json.loads(path.read_text())
        if not original["findings"]:
            continue
        (args.output / "O").mkdir(parents=True, exist_ok=True)
        (args.output / "O" / f"{path.stem}.txt").write_text(render(original, head))
        counts["O"] += 1
        enriched = json.loads((args.enriched / path.name).read_text())
        if enriched.get("has_enriched_keys"):
            (args.output / "E").mkdir(parents=True, exist_ok=True)
            (args.output / "E" / f"{path.stem}.txt").write_text(render(enriched, head))
            counts["E"] += 1
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
