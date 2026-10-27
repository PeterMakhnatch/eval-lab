"""MiMo weak-test witnesses and a frozen read-only Cloud DB issue projection."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from evallab.failure_library.records import DefectClass, Direction, Evidence, Finding, TaskRef
from evallab.failure_library.sources import (
    checked_file,
    defensive_summary,
    json_rows,
    make_record,
)
from evallab.failure_library.store import default_state_root

KEY = Path("research/answer-keys/mimo-weak-tests-v2.1")
MIMO_REPO = "https://huggingface.co/datasets/FineEnvs/MiMo-V2.6-RL-harbor-code"
MIMO_REVISION = "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785"
DB_EXPORT_SHA256 = "b6dd305c324dbe7990385e150002b82e20e60b7eb77baaa963eacdaa9a08d32b"
DB_QUERY = (
    "SELECT i.*, t.split AS task_split, t.metadata_json, t.cas_uri AS task_cas_uri "
    "FROM task_issues i JOIN corpus_tasks t USING(task_id) "
    "WHERE t.dataset_id = 'mimo-defect-map-v1' ORDER BY i.issue_id"
)


def _task(task_id: str, digest: str | None) -> TaskRef:
    return TaskRef(
        source_id="mimo-v2.6-rl",
        task_id=task_id,
        upstream_repo=MIMO_REPO,
        upstream_revision=MIMO_REVISION,
        task_path=f"tasks/{task_id}",
        task_version_digest=digest,
    )


def _grade_log(row: dict[str, Any], witness: dict[str, Any], state: Path) -> str | None:
    """Retain exact archived logs; a patch or a verbal execution claim is not a log."""
    descriptors = [row["primary"].get("grades_log", {})] + [
        scan.get("grades_log", {}) for scan in row.get("line", {}).get("scans", [])
    ]
    cited_paths = {
        ref.split("/grades.log", 1)[0] + "/grades.log"
        for ref in witness["evidence_refs"]
        if "/grades.log" in ref
    }
    for descriptor in descriptors:
        if descriptor.get("path") not in cited_paths or not descriptor.get("sha256"):
            continue
        digest = descriptor["sha256"].removeprefix("sha256:")
        relative = Path("raw/mimo-weak-tests") / MIMO_REVISION / "grades" / f"{digest}.log"
        cached = state / relative
        if not cached.is_file():
            original = Path(descriptor["path"]).expanduser()
            if not checked_file(original, digest):
                continue
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(original.read_bytes())
            cached.chmod(0o444)
        checked_file(cached, digest)
        # Source citations count LF-delimited lines; terminal output contains
        # bare CR progress updates that splitlines()/universal newlines expand.
        lines = cached.read_bytes().decode("utf-8").split("\n")
        for label, reward in (("full", 1), ("nop", 0), ("witness", 1)):
            control = witness["controls"][label]
            line = control["line"]
            if not (
                control["reward"] == reward and control["rc"] == 0 and control["status"] == "scored"
            ):
                raise ValueError(f"unusable MiMo control: {row['task_id']}/{witness['id']}/{label}")
            header = (
                re.fullmatch(r"== (.+) reward=([0-9.]+) rc=(-?\d+) (\w+)(?: .*)?", lines[line - 1])
                if 0 < line <= len(lines)
                else None
            )
            if not (
                header
                and header[1] == control["label"]
                and float(header[2]) == reward
                and int(header[3]) == 0
                and header[4] == "scored"
            ):
                raise ValueError(f"MiMo grade log does not bind recorded control: {cached}:{line}")
        return f"state://{relative}#L{witness['controls']['witness']['line']}"
    return None


def build_mimo_weak(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    state = state_root if state_root is not None else default_state_root()
    items = {
        item["subject"]["task_id"].removeprefix("mimo-v2.6-rl/"): item
        for item in json_rows(root / KEY / "items.jsonl")
    }
    findings = []
    for row in json_rows(root / KEY / "atlas.jsonl"):
        for witness in row.get("witnesses", []):
            item = items[row["task_id"]]
            if witness["package_digest"] != item["subject"]["task_digest"]:
                raise ValueError(f"MiMo witness/key package mismatch: {row['task_id']}")
            patch = root / KEY / witness["path"]
            if not checked_file(patch, witness["sha256"]):
                raise ValueError(f"missing complete MiMo witness patch: {patch}")
            source_url = f"{KEY}/atlas.jsonl#{row['task_id']}/witnesses/{witness['id']}"
            log = _grade_log(row, witness, state)
            evidence = (
                [
                    Evidence(
                        kind="executed",
                        receipt_ref=log,
                        reproducer_ref=str(KEY / witness["path"]),
                        note=(
                            "Retained historical grade logs report scored full=1, nop=0, "
                            "witness=1, rc=0 controls on the exact package; "
                            f"complete patch sha256:{witness['sha256']}. "
                            "The importer checks archived hashes and control headers, not "
                            "independent fresh execution or repaired-grader certification."
                        ),
                    )
                ]
                if log is not None
                else [
                    Evidence(
                        kind="static_finding",
                        url=source_url,
                        reproducer_ref=str(KEY / witness["path"]),
                        note=(
                            "Source-reported instruction-level review and complete patch retained, "
                            "but no hash-verified archived grade log is available. "
                            "Execution is not asserted."
                        ),
                    )
                ]
            )
            findings.append(
                make_record(
                    collection="mimo-weak-tests",
                    locator=f"mimo-weak-tests-v2.1:{row['task_id']}:{witness['id']}",
                    task=_task(row["task_id"], item["subject"]["task_digest"]),
                    defect_class="TS01",
                    direction="wrong_pass",
                    summary=defensive_summary("TS01", "wrong_pass", evidence),
                    evidence=evidence,
                    finder="Eval Lab Traces / instruction-adjudicated ref-subset",
                    source_url=source_url,
                    observed_at=row["primary"].get("finished_at", "2026-10-18"),
                    split=item["split"],
                    license="Apache-2.0 (dataset); witness code retains upstream project licenses",
                )
            )
    return findings


def _db_snapshot(state: Path, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    """First read captures the pinned logical export, never writes the live DB.

    A changed live database cannot silently redefine this collection. Existing
    raw snapshots remain sufficient after the live projection changes.
    """
    target = state / "raw/mimo-defect-map" / DB_EXPORT_SHA256 / "issues.jsonl"
    if not target.is_file():
        db = (
            db_path
            if db_path is not None
            else Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
        )
        try:
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                rows = [dict(row) for row in connection.execute(DB_QUERY)]
        except sqlite3.Error as exc:
            raise ValueError(f"cannot read pinned MiMo issue source: {db}: {exc}") from exc
        data = "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            for row in rows
        ).encode("utf-8")
        if hashlib.sha256(data).hexdigest() != DB_EXPORT_SHA256:
            raise ValueError(
                f"MiMo live issue export changed; restore the frozen raw snapshot {target}. "
                "A new source revision requires a separate reviewed collection update."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
    checked_file(target, DB_EXPORT_SHA256)
    return json_rows(target)


_ISSUE_CLASSES: dict[str, DefectClass] = {
    "weak_check": "TS01",
    "reference_validated_weak_test": "TS01",
    "grader_logic_bug": "TS02",
    "answer_reachable": "TS03",
    "grader_tamperable": "TS04",
    "unstated_requirement": "TS05",
    "instruction_grader_contradiction": "TS06",
    "over_strict_oracle": "TS07",
    "wrong_answer_key": "TS08",
    "environment_mismatch": "TS09",
    "infeasible": "TS10",
    "exploitable_llm_judge": "TS11",
}


def defect_map_records(rows: list[dict[str, Any]]) -> list[Finding]:
    findings = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        variants = metadata.get("variants", [])
        digests = {
            variant["original_package_digest"]
            for variant in variants
            if variant.get("original_package_digest")
        }
        if len(digests) != 1:
            raise ValueError(
                f"MiMo issue has ambiguous/missing package provenance: {row['issue_id']}"
            )
        source_url = (
            f"state://raw/mimo-defect-map/{DB_EXPORT_SHA256}/issues.jsonl#{row['issue_id']}"
        )
        tier = row["trust_tier"]
        if tier == "tier1_container_verified":
            confirmed = [
                variant
                for variant in variants
                if variant.get("confirmation_scope", {}).get("reference_validated_weak_test")
            ]
            if not confirmed or row["original_reward"] != 1 or row["repaired_reward"] != 0:
                raise ValueError(
                    f"tier1 issue lacks qualified differential provenance: {row['issue_id']}"
                )
            receipt = confirmed[0]["source_record"]
            evidence = [
                Evidence(
                    kind="executed",
                    receipt_ref=f"{receipt['cas_uri']}#{receipt['json_pointer']}",
                    note=(
                        "Retained Cloud DB source reports historical tier1_container_verified "
                        "qualification; original reward=1, repaired reward=0; "
                        f"witness={row['witness_digest']}; source file={receipt['source_file_digest']}. "
                        "Reference-validation and repair scope are source-reported, not "
                        "independently replayed or proof of a witnessed model exploit."
                    ),
                )
            ]
        elif tier == "tier2_consensus_labeled":
            evidence = [
                Evidence(
                    kind="judge_label",
                    label_source="Eval Lab source consensus / cloud task_issues tier2_consensus_labeled",
                    url=source_url,
                    note=(
                        f"Source-reported consensus label; issue={row['issue_id']}; "
                        f"witness={row['witness_digest']}; custody={row['cas_uri']}. "
                        "No human-review attestation or independent execution is established."
                    ),
                )
            ]
        elif tier == "tier3_provisional":
            evidence = [
                Evidence(
                    kind="static_finding",
                    url=source_url,
                    note=(
                        f"Source-reported provisional static finding; issue={row['issue_id']}; "
                        f"custody={row['cas_uri']}. No execution or consensus claim."
                    ),
                )
            ]
        else:
            raise ValueError(f"unknown MiMo issue trust tier: {tier}")
        # Local repairs are retained as provenance, not mislabeled upstream fixes.
        for variant in variants:
            receipt = variant.get("source_record", {})
            if receipt.get("cas_uri"):
                variant_source = variant["source"]
                if variant_source not in {"mimo30", "muse-r1", "muse-r2", "muse-w5"}:
                    variant_source = "retained at source locator"
                evidence.append(
                    Evidence(
                        kind="static_finding",
                        url=f"{receipt['cas_uri']}#{receipt.get('json_pointer', '')}",
                        note=(
                            f"source={variant_source}; source file={receipt.get('source_file_digest')}; "
                            f"original package={variant.get('original_package_digest')}; "
                            f"local repaired package={variant.get('repaired_package_digest')}; "
                            "local repair does not establish fixed_upstream status."
                        ),
                    )
                )
        direction: Direction = (
            row["direction"] if row["direction"] in {"wrong_pass", "unfair_fail"} else "other"
        )
        code: DefectClass = _ISSUE_CLASSES.get(row["defect_class"], "unclassified")
        native_status = row["status"]
        if native_status not in {"confirmed", "refuted"}:
            native_status = "retained at source locator"
        findings.append(
            make_record(
                collection="mimo-defect-map",
                locator=row["issue_id"],
                task=_task(row["task_id"], next(iter(digests))),
                defect_class=code,
                direction=direction,
                summary=(
                    f"{defensive_summary(code, direction, evidence)} "
                    f"Source-native historical issue outcome: {native_status}; "
                    "not independently confirmed or refuted by this importer."
                ),
                evidence=evidence,
                finder="Eval Lab retained task_issues / mimo-defect-map-v1",
                source_url=source_url,
                observed_at="2026-10-27",
                split=row["task_split"],
                license="Apache-2.0 (MiMo dataset); retained evidence has original source licenses",
            )
        )
    return findings


def build_mimo_defect_map(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    del root
    return defect_map_records(
        _db_snapshot(state_root if state_root is not None else default_state_root())
    )


COLLECTIONS = {
    "mimo-weak-tests": build_mimo_weak,
    "mimo-defect-map": build_mimo_defect_map,
}
