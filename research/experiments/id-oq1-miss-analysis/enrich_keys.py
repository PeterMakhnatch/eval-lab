"""ID-OQ-1: replace boilerplate MiMo scorer-key text with the key's own adjudicated evidence.

The OQ-6 scorer-only key cache carries `safe_summary` for every `mimo-weak-tests-v2.1:<task>:block|line-*` key, and
that string is one fixed sentence ("Archived instruction-adjudicated wrong-patch witness; ..."). A judge told to
match "the same specific causal grading contradiction" can never credit a finding against it. The answer key itself
(`research/answer-keys/mimo-weak-tests-v2.1/items.jsonl` + `findings-supported-v2.1.jsonl`) holds the adjudicated
evidence text and the instruction lines each key refers to. This script builds the scorer-side detail from them.
Scorer-side only: the auditor never sees it. DEV keys only; no model calls.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

KEYDIR = Path(__file__).resolve().parents[2] / "answer-keys/mimo-weak-tests-v2.1"
GENERIC_PREFIX = "Archived instruction-adjudicated wrong-patch witness"
KEY_ID = re.compile(r"mimo-weak-tests-v2\.1:([^:]+):(block|line-[0-9a-f]+)")
INSTRUCTION_REF = re.compile(r"^instruction\.md:([0-9,\-]+)$")
EVIDENCE_CHARS = 1400


def load_items() -> dict[str, list[str]]:
    items = {}
    for line in (KEYDIR / "items.jsonl").read_text().splitlines():
        row = json.loads(line)
        items[row["item_id"].split(":")[2].split("@")[0]] = row["evidence"]
    return items


def load_instruction_refs() -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    for line in (KEYDIR / "findings-supported-v2.1.jsonl").read_text().splitlines():
        row = json.loads(line)
        found = []
        for ref in row.get("evidence_refs", []):
            match = INSTRUCTION_REF.match(ref.rsplit("/", 1)[-1])
            if match and match[1] not in found:
                found.append(match[1])
        refs[row["id"]] = found
    return refs


def detail(key_id: str, items: dict[str, list[str]], refs: dict[str, list[str]]) -> str | None:
    match = KEY_ID.fullmatch(key_id)
    if match is None or match[1] not in items:
        return None
    task, kind = match[1], match[2]
    scope = "a block of the reference change" if kind == "block" else "one line of the reference change"
    lines = ", ".join(f"instruction.md:{r}" for r in refs.get(key_id, [])) or "the written instruction"
    evidence = " ".join(items[task])[:EVIDENCE_CHARS]
    return (
        f"Weak tests: a patch that drops {scope} still passes the grader, although the written requirement at "
        f"{lines} needs it. Adjudicated evidence for this task: {evidence}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundles", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    items, refs = load_items(), load_instruction_refs()
    enriched = total = 0
    for path in sorted(args.bundles.glob("D*.json")):
        bundle = json.loads(path.read_text())
        changed = False
        for key in bundle["keys"]:
            total += 1
            if (key["summary"] or "").startswith(GENERIC_PREFIX):
                text = detail(key["key_id"], items, refs)
                if text is not None:
                    key["summary_original"] = key["summary"]
                    key["summary"] = text
                    key["enriched"] = True
                    enriched += 1
                    changed = True
        bundle["has_enriched_keys"] = changed
        (args.output / path.name).write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
    print(json.dumps({"keys": total, "enriched": enriched}))


if __name__ == "__main__":
    main()
