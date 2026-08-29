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

# Every field the document is GOVERNED on: it must publish a canonical census
# block and each value must equal the live package. Token-searching for known
# stale strings cannot catch a NEW wrong value - changing 26 to 27 passed.
GOVERNED_CENSUS_FIELDS = (
    "trajectory_files_seen",
    "distinct_content_digests",
    "duplicate_paths_dropped",
    "agent_steps_unique",
    "clusters_with_agent_steps",
    "items_with_instruction_present",
    "context_complete",
    "raw_clusters",
    "n_items",
    "n_blockers",
)

CENSUS_BLOCK_RE = re.compile(r"<!--census-->\s*```(?:text)?\n(?P<body>.*?)```", re.DOTALL)


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
        "census": {
            "trajectory_files_seen": census["trajectory_files_seen"],
            "distinct_content_digests": census["distinct_content_digests"],
            "duplicate_paths_dropped": census["duplicate_paths_dropped"],
            "agent_steps_unique": census["agent_steps_unique"],
            "clusters_with_agent_steps": census["clusters_with_agent_steps"],
            "items_with_instruction_present": census["items_with_instruction_present"],
            "context_complete": census["context_completeness"].get("COMPLETE", 0),
            "raw_clusters": adequacy["raw_clusters"],
            "n_items": package["n_selected"],
            "n_blockers": len(readiness["blockers"]),
        },
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

    # Frontmatter must be PARSED, not searched. A READY frontmatter beside a
    # historical NOT_READY line passed the previous substring check.
    front = re.match(r"(?:<!--.*?-->\s*)*---\n(?P<body>.*?)\n---", doc, re.DOTALL)
    if front is None:
        violations.append("frontmatter block not found or malformed")
    else:
        fields: dict[str, str] = {}
        for row in front.group("body").split("\n"):
            if ":" in row and not row.startswith((" ", "-", "#")):
                key, _, value = row.partition(":")
                fields[key.strip()] = value.strip()
        expected = f"NOT_READY ({facts['n_blockers']} blockers)"
        if fields.get("readiness") != expected:
            violations.append(
                f"frontmatter readiness field is {fields.get('readiness')!r}, must be {expected!r}"
            )

    # Governed census block: every field compared to the live package, so a NEW
    # wrong value is caught rather than only a known-stale one.
    block = CENSUS_BLOCK_RE.search(doc)
    if block is None:
        violations.append(
            "doc must publish a canonical census block: an HTML comment "
            "<!--census--> followed by a fenced key: value block"
        )
    else:
        stated: dict[str, str] = {}
        for row in block.group("body").split("\n"):
            if ":" in row:
                key, _, value = row.partition(":")
                stated[key.strip()] = value.strip()
        for field in GOVERNED_CENSUS_FIELDS:
            want = facts["census"][field]
            got = stated.get(field)
            if got is None:
                violations.append(f"census block missing governed field {field!r}")
            elif got != str(want):
                violations.append(f"census block {field}={got!r}, live value is {want!r}")
        for extra in sorted(set(stated) - set(GOVERNED_CENSUS_FIELDS)):
            violations.append(
                f"census block states ungoverned field {extra!r}; add it to "
                f"GOVERNED_CENSUS_FIELDS or remove it"
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


def self_test() -> int:
    """Adversarial cases for the checker itself.

    Two bypasses it previously admitted, both from searching instead of parsing:
    a READY frontmatter beside a historical NOT_READY line, and a census value
    changed to a NEW wrong number that no stale-token list could contain.
    """
    facts = live_facts()
    blockers = "\n".join(facts["blockers"])
    census = "\n".join(f"{k}: {facts['census'][k]}" for k in GOVERNED_CENSUS_FIELDS)
    tail = f"{blockers}\nrating_contract_digest item_context_digest\n"
    n = facts["n_blockers"]
    good = (
        f"---\nreadiness: NOT_READY ({n} blockers)\n---\n<!--census-->\n```\n{census}\n```\n{tail}"
    )
    live_digests = facts["census"]["distinct_content_digests"]
    cases: list[tuple[str, str, bool]] = [
        (
            "READY frontmatter beside historical NOT_READY text",
            f"---\nreadiness: READY\n---\nreadiness: NOT_READY ({n} blockers) {HIST}\n{tail}",
            True,
        ),
        ("missing census block", f"---\nreadiness: NOT_READY ({n} blockers)\n---\n{tail}", True),
        (
            "census value changed to a NEW wrong number",
            good.replace(
                f"distinct_content_digests: {live_digests}",
                f"distinct_content_digests: {live_digests + 1}",
            ),
            True,
        ),
        ("well-formed frontmatter and census", good, False),
    ]
    failures = 0
    for name, doc, must_fail in cases:
        violations = check(doc, facts)
        rejected = bool(violations)
        ok = rejected is must_fail
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failures += 1
            for violation in violations[:3]:
                print(f"        {violation}")
    print("self-test: clean" if not failures else f"self-test: {failures} FAILED")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
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
