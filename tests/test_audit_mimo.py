"""Stored MiMo routing and evidence boundaries, without execution."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path


from evallab import audit_mimo
from evallab.registry import harbor_task_digest, task_directory_digest

TASK = "format-code-task-000792"
OTHER = "format-code-task-000788"
PACKAGE = "sha256:" + "a" * 64
VARIANT = "sha256:" + "b" * 64
HARBOR = "sha256:" + "c" * 64
LEDGER_COLUMNS = [
    "task_id", "split", "status", "reason", "run", "run_digest",
    "run_variant_status", "verdict", "verdict_evidence", "leak_channel",
    "census_label", "census_nop_job", "census_evidence",
]


def _csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _ledger(root: Path, **overrides: str) -> None:
    row = {
        "task_id": TASK, "split": "heldout", "status": "usable", "reason": "nop sound",
        "run": "original", "run_digest": PACKAGE, "run_variant_status": "",
        "verdict": "keep", "verdict_evidence": "stored strip candidate",
        "leak_channel": "unknown", "census_label": "sound", "census_nop_job": "nop-792",
        "census_evidence": "retained census label",
    }
    row.update(overrides)
    _csv(root / audit_mimo.LEDGER, LEDGER_COLUMNS, [row])


























def test_actual_package_uses_distinct_pins_and_rejects_stale_bytes(tmp_path: Path) -> None:
    task = tmp_path / audit_mimo.SNAPSHOT / "tasks" / TASK
    task.mkdir(parents=True)
    (task / "task.toml").write_text(f'[task]\nname = "mimo-v2.6-rl/{TASK}"\n', encoding="utf-8")
    (task / "instruction.md").write_text("Fix the task.\n", encoding="utf-8")
    package = task_directory_digest(task)
    harbor = harbor_task_digest(task)
    assert package != harbor
    _ledger(tmp_path, run_digest=package)
    provenance = task.parents[1] / "provenance.json"
    provenance.write_text(json.dumps({"license": "stored upstream license", "revision": audit_mimo.HF_SOURCE["revision"]}), encoding="utf-8")
    dataset, records = audit_mimo.read_stored_audit(tmp_path)
    selected = records[0].task
    assert dataset.license == "stored upstream license"
    assert selected.path == task.resolve()
    assert selected.package_digest == package
    assert selected.harbor_digest == harbor
    (task / "instruction.md").write_text("Different task bytes.\n", encoding="utf-8")
    _, records = audit_mimo.read_stored_audit(tmp_path)
    assert records[0].task.path is None
    assert records[0].task.package_digest == package
    assert records[0].task.parent_source["package_binding"] == "local package digest mismatch"
    assert records[0].verdict == "keep"












def test_mimo_static_uses_added_tests_and_excludes_corpus_harness(tmp_path: Path) -> None:
    package = tmp_path / audit_mimo.SNAPSHOT / "tasks" / TASK
    tests = package / "tests"
    tests.mkdir(parents=True)
    (package / "task.toml").write_text(f'[task]\nname = "mimo-v2.6-rl/{TASK}"\n', encoding="utf-8")
    (package / "instruction.md").write_text("Make the answer correct.\n", encoding="utf-8")
    (tests / "test_source.py").write_text("requests.get('https://example.invalid')\n", encoding="utf-8")
    patch = tests / "test.patch"
    patch.write_text(
        "diff --git a/test_answer.py b/test_answer.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_answer.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_answer():\n"
        "+    assert 1 == 1\n"
        "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/mimo_test_command.sh\n"
        "@@ -0,0 +1 @@\n"
        "+requests.get('https://example.invalid')\n",
        encoding="utf-8",
    )
    _ledger(tmp_path, run_digest=task_directory_digest(package))
    _, records = audit_mimo.read_stored_audit(tmp_path)
    observed = audit_mimo.static_observation(records[0].task)
    assert observed.status == "recorded"
    assert observed.facts["b_network"] is False
    patch_source = next(source for source in observed.sources if source.path == str(patch))
    assert patch_source.sha256 == "sha256:" + hashlib.sha256(patch.read_bytes()).hexdigest()
    assert observed.facts["package_digest_match"] is True
    assert audit_mimo.finalize_record(records[0].model_copy(update={"stages": {"static": observed}})).verdict == "keep"


