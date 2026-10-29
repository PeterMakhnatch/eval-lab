"""NP42-CE-1: counts only. Where the bytes behind ``task-package:<name>`` refs live, and what the compiler does with them.

Reads local FW-4 compiler outputs (``admission.jsonl`` before and after the resolver change) and the local task stores.
Prints and writes aggregate counts; no case id, task name or task text is written.

usage: np42_count.py CASES BASE_ADMISSION NEW_ADMISSION OUT.json STORE_LABEL=ROOT [STORE_LABEL=ROOT ...]
"""

import collections
import hashlib
import json
import re
import sys
from pathlib import Path

from evallab.registry import harbor_task_digest, task_directory_digest
from evallab.task_import import package_digest

cases_path, base_path, new_path, out_path, *stores = sys.argv[1:]
cases = {row["case_id"]: row for row in map(json.loads, Path(cases_path).read_text().splitlines())}
base = {row["case_id"]: row for row in map(json.loads, Path(base_path).read_text().splitlines())}
new = {row["case_id"]: row for row in map(json.loads, Path(new_path).read_text().splitlines())}
PACKAGE_REASONS = {"source_unavailable", "non_dev_input", "missing_input"}
confirmed = sorted(case_id for case_id, row in base.items() if set(row["reasons"]) & PACKAGE_REASONS)
SHAPE = re.compile(r"task-package:[A-Za-z0-9][A-Za-z0-9._-]*\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")


def package_ref(case_id: str) -> tuple[str, str]:
    refs = {k: v for k, v in cases[case_id]["source_digests"].items() if k.startswith("task-package:")}
    assert len(refs) == 1, case_id
    return next(iter(refs.items()))


refs = {case_id: package_ref(case_id) for case_id in confirmed}
names = sorted({ref.removeprefix("task-package:") for ref, _ in refs.values()})
result: dict[str, object] = {
    "cases": len(confirmed),
    "distinct_packages": len(names),
    "label_type": dict(collections.Counter(cases[c]["label_type"] for c in confirmed)),
    "polarity": dict(collections.Counter(cases[c]["polarity"] for c in confirmed)),
    "strict_dev_cases": sum(
        all(v == "DEV" for values in base[c]["split_checks"].values() for v in values) for c in confirmed
    ),
    "ref_shape_ok": sum(bool(SHAPE.fullmatch(ref)) for ref, _ in refs.values()),
    "digest_is_bare_hex64": sum(bool(HEX.fullmatch(digest)) for _, digest in refs.values()),
    "ref_has_fragment": sum("#" in ref for ref, _ in refs.values()),
    "base_reasons": dict(collections.Counter(base[c]["reasons"][0] for c in confirmed)),
    "new_reasons": dict(collections.Counter(
        new[c]["reasons"][0] if new[c]["reasons"] else "admitted" for c in confirmed
    )),
    "new_status": dict(collections.Counter(new[c]["status"] for c in confirmed)),
    "refusals_unchanged_for_sealed_and_missing_input": sorted(
        c for c in confirmed if base[c]["reasons"][0] in {"non_dev_input", "missing_input"}
    ) == sorted(c for c in confirmed if new[c]["reasons"] and new[c]["reasons"][0] in {"non_dev_input", "missing_input"}),
}
result["refusals_unchanged_for_sealed_and_missing_input"] = bool(result["refusals_unchanged_for_sealed_and_missing_input"])
exact = {ref.removeprefix("task-package:"): digest for ref, digest in refs.values()}
by_store: dict[str, dict[str, int]] = {}
for spec in stores:
    label, _, root_text = spec.partition("=")
    root = Path(root_text)
    row = collections.Counter()
    for name, digest in exact.items():
        directory = root / name
        if not directory.is_dir():
            row["name_absent"] += 1
            continue
        row["name_present"] += 1
        row["digest_match"] += task_directory_digest(directory).removeprefix("sha256:") == digest
    cases_matched = sum(
        (root / ref.removeprefix("task-package:")).is_dir()
        and task_directory_digest(root / ref.removeprefix("task-package:")).removeprefix("sha256:") == digest
        for ref, digest in refs.values()
    )
    row["cases_with_digest_match"] = cases_matched
    by_store[label] = dict(row)
result["by_store"] = by_store
if stores:
    first = Path(stores[0].partition("=")[2])
    functions = {
        "evallab.registry.task_directory_digest": task_directory_digest,
        "evallab.task_import.package_digest": package_digest,
        "evallab.registry.harbor_task_digest": harbor_task_digest,
    }
    result["digest_function_matches_of_distinct_packages"] = {
        label: sum(
            function(first / name).removeprefix("sha256:") == digest
            for name, digest in exact.items() if (first / name).is_dir()
        )
        for label, function in functions.items()
    }
result["sha256_of_cases_file"] = hashlib.sha256(Path(cases_path).read_bytes()).hexdigest()
Path(out_path).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n")
print(json.dumps(result, indent=1, sort_keys=True))
