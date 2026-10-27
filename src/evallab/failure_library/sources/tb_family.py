"""Terminal-Bench source findings, retaining version pins and evidence limits."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from evallab.failure_library.records import DefectClass, Direction, Evidence, Finding, TaskRef
from evallab.failure_library.sources import (
    checked_file,
    defensive_summary,
    json_rows,
    make_record,
)
from evallab.failure_library.store import default_state_root

KEYS = Path("research/answer-keys")
TB_REPO = "https://github.com/harbor-framework/terminal-bench"
TB4_REVISION = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
TB4_PINS = {"v4.0.0": TB4_REVISION, "1dcda8716784": "1dcda8716784493721921c23e4bc7f7d988b4494"}
PORT_REVISION = "a3ec9e22725b490a30b35d127bc9e7ac8a28de82"
PORT_REPO = "https://github.com/preferencemodel/terminal-bench-4-karotte-port"
ENVCHECK_REVISION = "12d4d094ef61c4898c16d18ebcc17beafce86035"
ENVCHECK_SHA256 = "893001c93945bd063ed2565145d846eb0e026a030f719ce4295ecebdde9039c9"
ENVCHECK_REPO = "https://github.com/tokenless-ai/envcheck-findings"
EPOCH_URL = "https://epoch.ai/benchmarks/terminal-bench-4/review"


def _tb4_task(item: dict[str, Any]) -> TaskRef:
    if item.get("scope") == "benchmark":
        return TaskRef(
            source_id="terminal-bench",
            task_id=item["subject"]["task_id"],
            upstream_repo=TB_REPO,
            upstream_revision=TB4_REVISION,
        )
    pin = item["package"]["pin"]
    return TaskRef(
        source_id="terminal-bench",
        task_id=item["subject"]["task_id"].removeprefix("terminal-bench/"),
        upstream_repo=TB_REPO,
        upstream_revision=TB4_PINS.get(pin, pin),
        task_path=f"tasks/{item['package']['name']}",
        task_version_digest=item["subject"].get("task_digest"),
    )


def build_pm_tb4(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    del state_root
    key = KEYS / "tb4-audit-v1"
    findings = []
    for item in json_rows(root / key / "items.jsonl"):
        evidence = [
            Evidence(
                kind="human_label",
                label_source="Preference Model",
                url=item["source_url"],
                note=(
                    "Historical Preference Model label; source execution assertions alone "
                    "are not independent verification or grounds for tier upgrades."
                ),
            )
        ]
        port = item.get("port", {})
        if port:
            port_status = port.get("status", "unknown")
            if port_status not in {"fixed", "na", "open", "partial", "wontfix"}:
                port_status = "unknown"
            evidence.append(
                Evidence(
                    kind="static_finding",
                    url=f"{PORT_REPO}/tree/{PORT_REVISION}",
                    note=(
                        f"Historical Karotte port status={port_status}; "
                        f"fix/port pin={PORT_REVISION}. This is a different repository, not a "
                        "confirmed repair of the pinned Harbor task or its current upstream head."
                    ),
                )
            )
        witness = item.get("witness")
        if witness and witness.get("confirmed"):
            if item["split"] != "dev":
                raise ValueError(f"PM executed witness outside DEV: {item['item_id']}")
            upstream, fixed = witness["upstream_replay"], witness["port_replay"]
            if not (
                upstream["status"] == "measured"
                and upstream["reward"] == 1
                and upstream.get("receipt_sha256")
                and fixed["mode"] == "full"
                and fixed["reward"] == 0
                and fixed.get("receipt_sha256")
            ):
                raise ValueError(f"incomplete PM witness receipt: {item['item_id']}")
            for name, digest in witness["files"].items():
                if not checked_file(root / key / witness["solution_dir"] / name, digest):
                    raise ValueError(f"missing PM witness file: {name}")
            evidence.append(
                Evidence(
                    kind="executed",
                    receipt_ref=f"{key}/witnesses.json#{witness['witness_id']}/upstream_replay",
                    reproducer_ref=item["reproducer"]["path"],
                    note=(
                        f"Retained historical receipt reports upstream reward=1; "
                        f"receipt={upstream['receipt_sha256']}; paired fixed-port "
                        f"full-mode reward=0; receipt={fixed['receipt_sha256']}. "
                        "This importer checks retained provenance, not independent fresh replay."
                    ),
                )
            )
        code: DefectClass = item["taxonomy_class_id"]
        # An ambiguity label alone says neither which correct submission failed
        # nor which incorrect one passed. Do not invent that direction.
        direction: Direction = "wrong_pass" if item.get("pm_types") == ["reward_hack"] else "other"
        findings.append(
            make_record(
                collection="pm-tb4-findings",
                locator=item["item_id"],
                task=_tb4_task(item),
                defect_class=code,
                direction=direction,
                summary=defensive_summary(code, direction, evidence),
                evidence=evidence,
                finder="Preference Model",
                source_url=item["source_url"],
                observed_at="2026-10-20",
                split=item["split"],
                license=None,
            )
        )
    return findings


def _envcheck_raw(root: Path, state_root: Path | None) -> list[dict[str, Any]]:
    state = state_root if state_root is not None else default_state_root()
    target = state / "raw" / "envcheck-tb4" / ENVCHECK_REVISION / "findings.jsonl"
    if not target.is_file():
        candidates = [
            root / "derived/kev/raw/envcheck-findings/data/findings.jsonl",
            Path.home()
            / "Developer/eval-lab/derived/kev/raw/envcheck-findings/data/findings.jsonl",
            Path.home()
            / "Developer/eval-lab-results/2026-10-20/tb4-audit-v1/scorer-source/tokenless-findings-source.jsonl",
        ]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            url = f"https://raw.githubusercontent.com/tokenless-ai/envcheck-findings/{ENVCHECK_REVISION}/data/findings.jsonl"
            with urlopen(url, timeout=60) as response:
                data = response.read()
        else:
            data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != ENVCHECK_SHA256:
            raise ValueError("EnvCheck source does not match the retained commit/hash")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o444)
    checked_file(target, ENVCHECK_SHA256)
    return json_rows(target)


def envcheck_records(items: list[dict[str, Any]], reported: list[dict[str, Any]]) -> list[Finding]:
    by_slug = {row["slug"]: row for row in reported if row["post"] == "2026-10-grader-defects"}
    findings = []
    categories: dict[str, DefectClass] = {
        "weak": "TS01",
        "reachable": "TS03",
        "tamperable": "TS04",
        "prompt_misaligned": "TS06",
    }
    for item in items:
        if item["expected_verdict"] != "fail":
            continue  # A sampled non-finding is not a broken-task finding.
        for cause in item["root_cause"]:
            epoch = item["label_strength"] == "epoch_flagged"
            code = categories.get(cause["category"], "unclassified")
            direction: Direction = "wrong_pass"
            if epoch:
                # Only the twelve issue-read overlays are admitted. The wider
                # thirty-row Epoch table is not bound to this key's task pins.
                direction = (
                    "unfair_fail" if "False Negative" in cause["source_class"] else "wrong_pass"
                )
                issue_number = cause["id"].rsplit("#", 1)[1]
                source_url = f"{TB_REPO}/issues/{issue_number}"
                evidence = [
                    Evidence(
                        kind="human_label",
                        label_source="Epoch AI",
                        url=source_url,
                        note=(
                            "Historical Epoch AI public issue label; not replayed or "
                            "independently execution-verified by this importer."
                        ),
                    )
                ]
                observed_at = "2026-10-15"
                finder = "Epoch AI"
            else:
                row = by_slug[cause["id"]]
                source_url = f"{ENVCHECK_REPO}/tree/{ENVCHECK_REVISION}/{row['files']}"
                task_id = item["package"]["name"]
                effect_cases = {
                    case["label"]: case
                    for case in row["cases"]
                    if case.get("task") == task_id
                    and case.get("shows") == "incorrect submission, accepted"
                    and case.get("grader_verdict", {}).get("status") == "graded"
                    and case.get("grader_verdict", {}).get("reward") == 1
                }
                receipts = [
                    ref
                    for ref in row.get("evidence", [])
                    if ref.get("artifact_kind") == "grader_output"
                    and ref.get("type") == "reproduction"
                    and ref.get("subject") in effect_cases
                    and ref.get("file")
                    and ref.get("sha256")
                ]
                evidence = [
                    Evidence(
                        kind="executed",
                        url=source_url,
                        receipt_ref=f"{ENVCHECK_REPO}/blob/{ENVCHECK_REVISION}/{row['files']}{ref['file']}",
                        note=(
                            "Publisher-reported historical wrong-pass execution; "
                            f"reported receipt sha256:{ref['sha256']}. "
                            "Receipt metadata is a source assertion, not independently "
                            "verified execution. No replay was performed by this importer."
                        ),
                    )
                    for ref in receipts
                ] or [
                    Evidence(
                        kind="static_finding",
                        url=source_url,
                        note="Source finding retained without a task-matched graded wrong-pass receipt.",
                    )
                ]
                if row["defect"].get("family") == "checker-logic" and cause["category"] == "weak":
                    code = "TS02"
                observed_at = row["discovered_by"]["date"]
                finder = "Tokenless AI / EnvCheck"
            findings.append(
                make_record(
                    collection="envcheck-tb4",
                    locator=cause["id"],
                    task=_tb4_task(item),
                    defect_class=code,
                    direction=direction,
                    summary=defensive_summary(code, direction, evidence),
                    evidence=evidence,
                    finder=finder,
                    source_url=source_url,
                    observed_at=observed_at,
                    split=item["split"],
                    license="CC-BY-4.0",
                )
            )
    return findings


def build_envcheck(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    return envcheck_records(
        json_rows(root / KEYS / "envcheck-tb4-39/items.jsonl"), _envcheck_raw(root, state_root)
    )


def build_tb21_weak(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    del state_root
    key = KEYS / "tb21-weak-tests-v1"
    items = {item["subject"]["task_id"]: item for item in json_rows(root / key / "items.jsonl")}
    atlas = {row["task_id"]: row for row in json_rows(root / key / "atlas.jsonl")}
    findings = []
    for reported in json_rows(root / key / "findings.jsonl"):
        item = items[reported["subject"]["task_id"]]
        task_id = reported["subject"]["task_id"].removeprefix("tb21/")
        row = atlas[task_id]
        witness = next(w for w in row["witnesses"] if w["id"] == reported["id"])
        receipts_present = True
        for label, reward in (
            ("original_reference", 1),
            ("nop", 0),
            ("full_targets", 1),
            ("witness", 1),
        ):
            control = witness["controls"][label]
            if not (
                control["reward"] == reward
                and control["status"] == "scored"
                and control["package_digest"] == item["subject"]["task_digest"]
                and control["agent_success"]
                and control["host_isolation_applied"]
                and control["task_timeout_preserved"]
            ):
                raise ValueError(f"invalid TB21 control: {reported['id']}/{label}")
            path = root / key / control["evidence_path"]
            present = checked_file(path, control["evidence_sha256"])
            receipts_present &= present
            if present:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                if receipt["trial_result"]["verifier_result"]["rewards"]["reward"] != reward:
                    raise ValueError(f"TB21 receipt reward disagrees: {path}")
        control = witness["controls"]["witness"]
        evidence = (
            [
                Evidence(
                    kind="executed",
                    receipt_ref=str(key / control["evidence_path"]),
                    reproducer_ref=reported["reproducer"]["path"],
                    note=(
                        "Archived native receipts report original-reference=1, nop=0, "
                        "full-targets=1, witness=1; "
                        f"witness receipt={control['evidence_sha256']}. "
                        "No independent fresh replay was performed by this importer."
                    ),
                )
            ]
            if receipts_present
            else [
                Evidence(
                    kind="static_finding",
                    url=f"{key}/atlas.jsonl#{task_id}",
                    note="Instruction-adjudicated weak-test witness; native control receipts missing in this checkout, so execution is not asserted.",
                )
            ]
        )
        findings.append(
            make_record(
                collection="tb21-weak-tests",
                locator=reported["id"],
                task=TaskRef(
                    source_id="terminal-bench-2-1",
                    task_id=task_id,
                    upstream_repo="https://github.com/harbor-framework/terminal-bench-2-1",
                    upstream_revision=item["source_pin"],
                    task_path=f"tasks/{task_id}",
                    task_version_digest=item["subject"]["task_digest"],
                ),
                defect_class="TS01",
                direction="wrong_pass",
                summary=defensive_summary("TS01", "wrong_pass", evidence),
                evidence=evidence,
                finder="Eval Lab Traces / native ref-subset controls",
                source_url=f"{key}/findings.jsonl#{reported['id']}",
                observed_at="2026-10-19",
                split=item["split"],
                license="Apache-2.0",
            )
        )
    return findings


# These mechanisms are supported by the actual before/after diff summaries.
# Version changes and resource bumps alone do not establish a taxonomy class.
_FIX_CLASSES: dict[str, tuple[DefectClass, Direction]] = {
    "adaptive-rejection-sampler": ("TS05", "unfair_fail"),
    "build-pmars": ("TS05", "unfair_fail"),
    "caffe-cifar-10": ("TS07", "unfair_fail"),
    "configure-git-webserver": ("TS06", "unfair_fail"),
    "fix-git": ("TS03", "wrong_pass"),
    "hf-model-inference": ("TS07", "unfair_fail"),
    "polyglot-c-py": ("TS07", "unfair_fail"),
    "polyglot-rust-c": ("TS07", "unfair_fail"),
    "query-optimize": ("TS05", "unfair_fail"),
}


def build_tb2_fixes(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    del state_root
    source_path = Path("research/experiments/breaker-rediscovery-v1/tb2-diff.json")
    source = json.loads((root / source_path).read_text(encoding="utf-8"))
    before, after = source["old_pin"], source["new_pin"]
    changes = {row["task_id"]: row for row in source["tasks"] if row["in_pr53_26"]}
    findings = []
    for item in json_rows(root / KEYS / "tb2-to-tb21-v1/items.jsonl"):
        if item["role"] != "child" or item["subsplit"] != "pr53_modified":
            continue
        task_id = item["subject"]["task_id"].removeprefix("tb2/")
        change = changes[task_id]
        code, direction = _FIX_CLASSES.get(task_id, ("unclassified", "other"))
        fix_url = f"https://github.com/{after['repo']}/tree/{after['commit']}/tasks/{task_id}"
        evidence = [
            Evidence(
                kind="upstream_fix",
                url=fix_url,
                fix_revision=after["commit"],
                note=(
                    f"Before={before['repo']}@{before['commit']}; "
                    f"after={after['repo']}@{after['commit']}; "
                    f"after package={change['new_package_digest']}; "
                    f"diff={source_path}#{task_id}. No executed differential reproducer claimed."
                ),
            )
        ]
        findings.append(
            make_record(
                collection="tb2-to-tb21-fixes",
                locator=item["item_id"],
                task=TaskRef(
                    source_id="terminal-bench-2",
                    task_id=task_id,
                    upstream_repo=f"https://github.com/{before['repo']}",
                    upstream_revision=before["commit"],
                    task_path=task_id,
                    task_version_digest=item["subject"]["task_digest"],
                ),
                defect_class=code,
                direction=direction,
                summary=defensive_summary(code, direction, evidence),
                evidence=evidence,
                finder="Terminal-Bench maintainers / PR #53 version-diff cohort",
                source_url=f"{source_path}#{task_id}",
                observed_at="2026-10-27",
                status_now="fixed_upstream",
                fixed_in_revision=after["commit"],
                split=item["split"],
                license=None,
            )
        )
    return findings


COLLECTIONS = {
    "pm-tb4-findings": build_pm_tb4,
    "envcheck-tb4": build_envcheck,
    "tb21-weak-tests": build_tb21_weak,
    "tb2-to-tb21-fixes": build_tb2_fixes,
}
