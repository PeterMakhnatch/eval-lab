"""Mechanical protocol-document consistency check.

WHY THIS EXISTS
    Across revisions 3-11 the protocol document repeatedly carried stale digests
    and stale arithmetic that hand-patching missed. A narrow frontmatter-count
    test caught exactly one class and nothing else, and reviewers kept finding the
    rest by eye. This check derives EVERY hash and numeric claim from the live
    package and fails on any drift, so the document cannot disagree with the
    artifact it describes.

HISTORICAL VALUES
    The document legitimately cites superseded values when explaining a
    correction. Those lines MUST carry the marker `<!--hist-->`. An unmarked stale
    value is a defect; a marked one is deliberate narrative. The marker is
    machine-readable and self-documenting.

USAGE
    python3 research/goldset/check_doc_consistency.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DOC = HERE / "GOLDSET-ITEM-SELECTION-AND-TAXONOMY-2026-08-28.md"
PACKAGE = HERE / "labeling_package.json"
HIST = "<!--hist-->"

# Values from superseded revisions that must never appear unmarked.
SUPERSEDED_TOKENS = ("167", "237", "13.84", "13.8 %", "13.8%", "149/183", "81.4")


def live_facts() -> dict[str, Any]:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    readiness = package["readiness"]
    adequacy = readiness["cluster_adequacy"]
    census = package["census"]
    sizes = list(census["agent_steps_per_cluster"].values())
    total = sum(sizes)
    return {
        "file_sha256": hashlib.sha256(PACKAGE.read_bytes()).hexdigest(),
        "package_digest": package["package_digest"],
        "build_id": package["build_id"],
        "rating_contract_digest": readiness["authentication"]["rating_contract_digest"],
        "n_items": package["n_selected"],
        "n_clusters": adequacy["raw_clusters"],
        "k_eff": adequacy["effective_clusters_kish"],
        "kish_numerator": total,
        "kish_denominator": sum(n * n for n in sizes),
        "max_cluster": max(sizes),
        "concentration_pct": 100.0 * max(sizes) / total,
        "n_blockers": len(readiness["blockers"]),
        "complete": census["context_completeness"].get("COMPLETE", 0),
        "blockers": list(readiness["blockers"]),
    }


def check(doc: str, facts: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    allowed = {
        facts["file_sha256"],
        facts["package_digest"],
        facts["build_id"],
        facts["rating_contract_digest"],
    }
    allowed_short = {digest[:8] for digest in allowed}

    for lineno, line in enumerate(doc.split("\n"), 1):
        if HIST in line:
            continue

        for digest in re.findall(r"\b[0-9a-f]{64}\b", line):
            if digest not in allowed:
                violations.append(
                    f"L{lineno}: stale/unknown digest {digest[:12]}… "
                    f"(mark historical lines with {HIST})"
                )
        for short in re.findall(r"\b([0-9a-f]{8})…", line):
            if short not in allowed_short:
                violations.append(f"L{lineno}: stale abbreviated digest {short}…")

        for value in re.findall(r"K_?(?:\{?\\?text\{?eff\}?\}?)?\s*=\s*([0-9]+\.[0-9]+)", line):
            if abs(float(value) - facts["k_eff"]) > 0.005:
                violations.append(f"L{lineno}: K_eff={value}, live is {facts['k_eff']}")

        for num, den in re.findall(r"([0-9]{2,6})\^2\}?\}?\{?([0-9]{3,7})", line):
            if (int(num), int(den)) != (
                facts["kish_numerator"],
                facts["kish_denominator"],
            ):
                violations.append(
                    f"L{lineno}: Kish {num}^2/{den}, live is "
                    f"{facts['kish_numerator']}^2/{facts['kish_denominator']}"
                )

        for num, den in re.findall(
            r"\b([0-9]{1,4})\s*(?:of|/)\s*([0-9]{2,5})\b(?=[^0-9]{0,12}item)", line
        ):
            if int(den) != facts["n_items"]:
                violations.append(
                    f"L{lineno}: '{num} of {den} items', live count is {facts['n_items']}"
                )

        if re.search(r"concentrat|carries|share", line, re.IGNORECASE):
            for value in re.findall(r"([0-9]{1,2}\.[0-9])\s*%", line):
                if abs(float(value) - facts["concentration_pct"]) > 0.06:
                    violations.append(
                        f"L{lineno}: concentration {value}%, live is "
                        f"{facts['concentration_pct']:.1f}%"
                    )

        for token in SUPERSEDED_TOKENS:
            if re.search(rf"(?<![0-9.]){re.escape(token)}(?![0-9])", line):
                violations.append(
                    f"L{lineno}: superseded value {token!r} unmarked "
                    f"(live items={facts['n_items']}, K_eff={facts['k_eff']})"
                )

    if f"readiness: NOT_READY ({facts['n_blockers']} blockers)" not in doc:
        violations.append(
            f"frontmatter must state 'readiness: NOT_READY ({facts['n_blockers']} blockers)'"
        )
    for blocker in facts["blockers"]:
        if blocker not in doc:
            violations.append(f"live blocker text absent from doc: {blocker}")
    for name in ("rating_contract_digest", "item_context_digest"):
        if name not in doc:
            violations.append(f"doc must document the {name} contract field")
    if "item_set_digest" in doc and doc.count(HIST) == 0:
        violations.append("doc names retired field item_set_digest without a marker")

    return sorted(set(violations))


def main() -> int:
    facts = live_facts()
    violations = check(DOC.read_text(encoding="utf-8"), facts)
    if violations:
        print(f"{len(violations)} doc-consistency violations:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("doc consistency: clean")
    print(
        f"  items={facts['n_items']} clusters={facts['n_clusters']} "
        f"K_eff={facts['k_eff']} concentration={facts['concentration_pct']:.1f}% "
        f"blockers={facts['n_blockers']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
