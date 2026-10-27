"""Pinned, metadata-only imports of SWE task quality evidence.

Builders never execute dataset programs. Published human/judge labels stay labels;
only a pinned differential result is admitted as executed evidence.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import urllib.request
import zipfile
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from evallab.failure_library.records import (
    Evidence,
    Finding,
    Split,
    TaskRef,
    derive_tier,
    make_finding_id,
)
from evallab.failure_library.store import default_state_root

OBSERVED = date(2026, 10, 27)
SWE_ORIGINAL_REVISION = "f5351ee8c6663736817027db3ad03fe662cb5bb8"
SWE_VERIFIED_REVISION = "c104f840cc67f8b6eec6f759ebc8b2693d585d4a"
PATCHDIFF_TASK_REVISION = "944835eeebcccbe32d1be2f027e696daae60c565"
PRO_ORIGINAL_REVISION = "307bd006a0a3807424d3c5525eaf98ca8e63aed3"
PRO_FIXED_REVISION = "ae466f824cdee69d2c010b6c954b7418c411b6e4"
UTBOOST_REVISION = "4f32782517f23940d3ede8cd1f250667873cc0c6"
OPENAI_URL = (
    "https://cdn.openai.com/introducing-swe-bench-verified/swe-bench-annotation-results.zip"
)
OPENAI_SHA = "b9297c31f6c2ef712218b5bb2830bd47a7f600dedf6b927191d0170177266ab9"
PRO_README_SHA = "ac6a2ef14e99d1b2ef33c17a8fe3448c05e70bb66c4f742448e48c50f7b8a53a"
UTBOOST_SHA = "66cc8846b3e7585853ef57deb39b1698acf4ad17d83c740968e0517fb9e46619"
PATCHDIFF_URL = "https://zenodo.org/api/records/18258368/files/PatchDiff_0115_1.zip/content"
PATCHDIFF_SHA = "32df17a1d86f65c3f7bcebbee8b196467198a4a0ec4b59aa83616426bf2aee1a"
PATCHDIFF_MEMBERS = {
    "CodeStory": "30b2e153be517c08731a5ba273ebd0df91c509b7fd17d5cce12d5fb5acee033d",
    "LearnByInteract": "b6471f1b6a4d66cb3dc08c001d2a908e9a9ca179f273edb4083bfa20cd004cf7",
    "OpenHands": "9e6913c0f169d5d62261ba8266400675cdbda3f531cd6d9cc3a92d5b6465421c",
}
ABA_PINS = {
    "tb2": "4f96efeb94466ab848ded6d5f48fdc3bbaf0f4f0b6df9b4b376cd2996d3bddf8",
    "swev": "06f115a5588aa38a859fe2e2ee1d28222b448a9e45776c3cc2e85c72bc8ada0b",
    "swepro": "5999bac9cb2306b576ebc2724ca8588fffe2b37b277990d3b6f42538fd386dae",
}


def pinned_bytes(
    collection: str,
    revision: str,
    name: str,
    sha256: str,
    *,
    state_root: Path | None = None,
    url: str | None = None,
    local: Path | None = None,
) -> bytes:
    """Fetch once, verify the pin, then retain immutable source bytes outside Git."""
    state = state_root if state_root is not None else default_state_root()
    path = state / "raw" / collection / revision / name
    if path.is_file():
        data = path.read_bytes()
    elif local is not None and local.is_file():
        data = local.read_bytes()
    elif url is not None:
        request = urllib.request.Request(url, headers={"User-Agent": "evallab-failure-library/1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
    else:
        raise FileNotFoundError(f"Pinned source missing: {path}; seed it from {local}")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"Source SHA-256 mismatch: {collection}/{revision}/{name}")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
        path.chmod(0o444)
    return data


class SplitPolicy:
    """Existing reported splits win; new task families use a shared 80/20 hash."""

    def __init__(self, root: Path) -> None:
        self.assignments: dict[str, set[str]] = {}
        path = root / "research/answer-keys/splits/assignments.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                name = row["task"].split("@", 1)[0].rsplit("/", 1)[-1]
                split = row["split"].lower()
                if "cheatbench" in row["key_id"].lower() or "hackbench" in row["key_id"].lower():
                    split = "cross_test"
                self.assignments.setdefault(name, set()).add(split)

    def get(self, task_id: str, group: str) -> Split:
        bare = task_id.split("@", 1)[0].rsplit("/", 1)[-1]
        existing = self.assignments.get(bare, set())
        precedence: tuple[tuple[str, Split], ...] = (
            ("cross_test", "cross_test"),
            ("heldout", "heldout"),
            ("test", "TEST"),
            ("dev", "DEV"),
        )
        for native, result in precedence:
            if native in existing:
                return result
        return "heldout" if int(hashlib.sha256(group.encode()).hexdigest(), 16) % 5 == 0 else "DEV"


def source_finding(
    collection: str,
    task: TaskRef,
    defect_class: str,
    locator: str,
    summary: str,
    evidence: list[Evidence],
    *,
    finder: str,
    source_url: str,
    split: Split,
    direction: str = "other",
    license: str | None = None,
    fixed_in_revision: str | None = None,
) -> Finding:
    return Finding.model_validate(
        {
            "finding_id": make_finding_id(
                task.source_id, task.task_id, task.upstream_revision or "", defect_class, locator
            ),
            "collection": collection,
            "task": task,
            "defect_class": defect_class,
            "locator": locator,
            "summary": summary[:1000],
            "evidence": evidence,
            "tier": derive_tier(item.kind for item in evidence),
            "finder": finder,
            "source_url": source_url,
            "observed_at": OBSERVED,
            "split": split,
            "direction": direction,
            "license": license,
            "status_now": "fixed_upstream" if fixed_in_revision else "unknown",
            "fixed_in_revision": fixed_in_revision,
        }
    )


def swe_task(task_id: str, *, original: bool = False) -> TaskRef:
    dataset = "SWE-bench" if original else "SWE-bench_Verified"
    return TaskRef(
        source_id="swe-bench",
        task_id=task_id,
        upstream_repo=f"https://huggingface.co/datasets/princeton-nlp/{dataset}",
        upstream_revision=SWE_ORIGINAL_REVISION if original else SWE_VERIFIED_REVISION,
        task_path=f"test/instance_id={task_id}",
    )


def parse_openai(rows: Iterable[Mapping[str, str]], policy: SplitPolicy) -> list[Finding]:
    """Maximum severity 2/3 is a material issue, not an executed failure."""
    findings = []
    for row in rows:
        task_id = row["instance_id"]
        axes = (("underspecified", "TS05"), ("false_negative", "TS07"))
        for axis, code in axes:
            severity = float(row[axis])
            if severity not in (0, 1, 2, 3):
                raise ValueError(f"Unknown OpenAI severity: {severity}")
            if severity < 2:
                continue
            note = row.get(axis + "_notes", "").strip()
            locator = f"ensembled_annotations_public.csv/{task_id}/{axis}"
            findings.append(
                source_finding(
                    "openai-swebench-verified-annotations",
                    swe_task(task_id, original=True),
                    code,
                    locator,
                    f"OpenAI 2024 human ensemble: {axis}, severity {int(severity)}/3. {note}",
                    [
                        Evidence(
                            kind="human_label",
                            label_source=OPENAI_URL + "#" + locator,
                            note="Association-only binding to the canonical historical task snapshot; "
                            "the annotation release does not pin audited task-package bytes.",
                        )
                    ],
                    finder="OpenAI 2024 human annotators (maximum of three ratings)",
                    source_url=OPENAI_URL,
                    split=policy.get(task_id, "swe-bench/" + task_id),
                    direction="unfair_fail",
                )
            )
        if float(row.get("other_major_issues", "0")) == 1:
            locator = f"ensembled_annotations_public.csv/{task_id}/other_major_issues"
            findings.append(
                source_finding(
                    "openai-swebench-verified-annotations",
                    swe_task(task_id, original=True),
                    "unclassified",
                    locator,
                    "OpenAI 2024 human ensemble: other_major_issues. " + row.get("other_notes", ""),
                    [
                        Evidence(
                            kind="human_label",
                            label_source=OPENAI_URL + "#" + locator,
                            note="Association-only binding to the canonical historical task snapshot; "
                            "the annotation release does not pin audited task-package bytes.",
                        )
                    ],
                    finder="OpenAI 2024 human annotators",
                    source_url=OPENAI_URL,
                    split=policy.get(task_id, "swe-bench/" + task_id),
                )
            )
    return sorted(findings, key=lambda item: item.finding_id)


def build_openai(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    raw = pinned_bytes(
        "openai-swebench-verified-annotations",
        "2024-08-12",
        "swe-bench-annotation-results.zip",
        OPENAI_SHA,
        state_root=state_root,
        url=OPENAI_URL,
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        text = archive.read("ensembled_annotations_public.csv").decode("utf-8-sig")
    return parse_openai(csv.DictReader(io.StringIO(text)), SplitPolicy(root))


def parse_pro_corrections(text: str, policy: SplitPolicy) -> list[Finding]:
    categories = {
        "Misleading prompts": ("TS06", "unfair_fail"),
        "Overly narrow tests": ("TS05", "unfair_fail"),
        "Overly borad tests": ("TS01", "wrong_pass"),  # Upstream spelling.
        "Other issues": ("unclassified", "other"),
    }
    findings = []
    seen: set[str] = set()
    pattern = r"- \*\*([^*]+)\*\* \((\d+) samples\)\s*```text\n(.*?)```"
    for match in re.finditer(pattern, text, re.S):
        category, declared, body = match.groups()
        code, direction = categories[category]
        task_ids = body.split()
        if len(task_ids) != int(declared):
            raise ValueError("Pro correction category count disagrees with its task list")
        for task_id in task_ids:
            if task_id in seen:
                raise ValueError("Duplicate Pro correction task identity")
            seen.add(task_id)
            task = TaskRef(
                source_id="swebench-pro",
                task_id=task_id,
                upstream_repo="https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro",
                upstream_revision=PRO_ORIGINAL_REVISION,
                task_path="test/instance_id=" + task_id,
            )
            url = (
                "https://huggingface.co/datasets/opencompass/SWEBench-Pro-Verified/blob/"
                + PRO_FIXED_REVISION
                + "/README.md"
            )
            findings.append(
                source_finding(
                    "swebench-pro-verified-corrections",
                    task,
                    code,
                    f"README.md/{category}/{task_id}",
                    f"Expert-approved source correction: {category}; original Scale v1 task.",
                    [
                        Evidence(kind="human_label", label_source=url + "#" + task_id),
                        Evidence(
                            kind="upstream_fix",
                            fix_revision=PRO_FIXED_REVISION,
                            url=url,
                            note="Corrected dataset is opencompass/SWEBench-Pro-Verified, not Scale V2.",
                        ),
                    ],
                    finder="opencompass expert task reviewers",
                    source_url=url,
                    split=policy.get(task_id, "swebench-pro/" + task_id),
                    direction=direction,
                    license="apache-2.0",
                    fixed_in_revision=PRO_FIXED_REVISION,
                )
            )
    if not findings:
        raise ValueError("No Pro correction categories found")
    return sorted(findings, key=lambda item: item.finding_id)


def build_pro_corrections(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    url = (
        "https://huggingface.co/datasets/opencompass/SWEBench-Pro-Verified/resolve/"
        + PRO_FIXED_REVISION
        + "/README.md"
    )
    raw = pinned_bytes(
        "swebench-pro-verified-corrections",
        PRO_FIXED_REVISION,
        "README.md",
        PRO_README_SHA,
        state_root=state_root,
        url=url,
    )
    return parse_pro_corrections(raw.decode(), SplitPolicy(root))


def parse_utboost(rows: Mapping[str, Any], policy: SplitPolicy) -> list[Finding]:
    findings = []
    for task_id, row in sorted(rows.items()):
        if not isinstance(row["aug_test"], str) or not row["aug_test"]:
            raise ValueError("UTBoost confirmed task has no augmented test patch")
        if not isinstance(row["augFail2Pass"], list):
            raise ValueError("UTBoost augmented test identifiers must be a list")
        url = (
            f"https://github.com/CUHK-Shenzhen-SE/UTBoost/blob/{UTBOOST_REVISION}"
            "/assets/useful_scripts/augTest.json"
        )
        findings.append(
            source_finding(
                "utboost-weak-tasks",
                swe_task(task_id, original=True),
                "TS01",
                "assets/useful_scripts/augTest.json/" + task_id,
                "UTBoost authors confirmed insufficient original tests; published augmented patch "
                f"with {len(row['augFail2Pass'])} augmented fail-to-pass identifiers. "
                "No task-bound candidate-patch execution receipt is included in this import.",
                [
                    Evidence(
                        kind="human_label",
                        label_source=url + "#" + task_id,
                        note="Association-only canonical task binding; augmented tests alone are not "
                        "an executed reproducer of a wrong pass.",
                    )
                ],
                finder="UTBoost authors",
                source_url=url,
                split=policy.get(task_id, "swe-bench/" + task_id),
                direction="wrong_pass",
                license="MIT (repository); inherited task rights apply",
            )
        )
    return findings


def build_utboost(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    raw = pinned_bytes(
        "utboost-weak-tasks",
        UTBOOST_REVISION,
        "augTest.json",
        UTBOOST_SHA,
        state_root=state_root,
        url=f"https://raw.githubusercontent.com/CUHK-Shenzhen-SE/UTBoost/{UTBOOST_REVISION}"
        "/assets/useful_scripts/augTest.json",
    )
    return parse_utboost(json.loads(raw), SplitPolicy(root))


def parse_patchdiff(by_tool: Mapping[str, Mapping[str, Any]], policy: SplitPolicy) -> list[Finding]:
    findings = []
    for tool, rows in sorted(by_tool.items()):
        member = f"PatchDiff/results/RQ1_{tool}_runall.json"
        for task_id, row in sorted(rows.items()):
            if row["difference"] not in {"functionality", "coding_conventions"}:
                raise ValueError("Unknown PatchDiff differential result")
            tests = row["oracle_pass_model_fail"]
            if row["difference"] != "functionality" or not tests:
                continue
            locator = f"{member}/{task_id}"
            findings.append(
                source_finding(
                    "patchdiff-wrong-passes",
                    TaskRef(
                        source_id="swe-bench",
                        task_id=task_id,
                        upstream_repo="https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified",
                        upstream_revision=PATCHDIFF_TASK_REVISION,
                        task_path=f"data/test-00000-of-00001.parquet#instance_id={task_id}",
                    ),
                    "TS01",
                    locator,
                    f"PatchDiff published functional differential wrong pass: {tool}; "
                    f"{len(tests)} developer tests pass the oracle and fail the prior-passing patch.",
                    [
                        Evidence(
                            kind="executed",
                            receipt_ref=PATCHDIFF_URL + "#" + locator,
                            note="Published RQ1 execution result, not a fresh Eval Lab replay; "
                            "convention-only differences excluded.",
                        )
                    ],
                    finder="PatchDiff authors",
                    source_url="https://zenodo.org/records/18258368",
                    split=policy.get(task_id, "swe-bench/" + task_id),
                    direction="wrong_pass",
                    license="cc-by-4.0",
                )
            )
    return sorted(findings, key=lambda item: item.finding_id)


def build_patchdiff(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    raw = pinned_bytes(
        "patchdiff-wrong-passes",
        "zenodo-18258368",
        "PatchDiff_0115_1.zip",
        PATCHDIFF_SHA,
        state_root=state_root,
        url=PATCHDIFF_URL,
    )
    by_tool = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for tool, digest in PATCHDIFF_MEMBERS.items():
            data = archive.read(f"PatchDiff/results/RQ1_{tool}_runall.json")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("PatchDiff member SHA-256 mismatch")
            by_tool[tool] = json.loads(data)
    return parse_patchdiff(by_tool, SplitPolicy(root))


def parse_aba(bench: str, rows: Iterable[Mapping[str, Any]], policy: SplitPolicy) -> list[Finding]:
    findings = []
    for row in rows:
        if row["source"] not in {"aba_static", "aba_traj"} or row.get("positive") is not True:
            continue
        task_id = row["task_id"]
        if bench == "swev":
            task = swe_task(task_id)
            group = "swe-bench/" + task_id
        elif bench == "swepro":
            task = TaskRef(
                source_id="swebench-pro",
                task_id=task_id,
                upstream_repo="https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro",
                upstream_revision=PRO_ORIGINAL_REVISION,
                task_path="test/instance_id=" + task_id,
            )
            group = "swebench-pro/" + task_id
        elif bench == "tb2":
            task = TaskRef(
                source_id="terminal-bench-2",
                task_id=task_id,
                upstream_repo="https://github.com/harbor-framework/terminal-bench-2",
                upstream_revision="69671fbaac6d67a7ef0dfec016cc38a64ef7a77c",
                task_path=task_id,
            )
            group = "terminal-bench/" + task_id
        else:
            raise ValueError(f"Unknown ABA benchmark: {bench}")
        for code in sorted(set(row["classes"])) or ["unclassified"]:
            findings.append(
                source_finding(
                    "aba-audits",
                    task,
                    code,
                    f"{bench}/{row['source']}/{task_id}/{code}",
                    "UNVERIFIED ABA LLM audit: " + row["raw_label"],
                    [
                        Evidence(
                            kind="judge_label",
                            label_source=row["ref"],
                            note="UNVERIFIED; association-only canonical snapshot binding, not proof "
                            "of audited task-package equivalence or independent execution.",
                        )
                    ],
                    finder="Auto Benchmark Audit LLM",
                    source_url=row["ref"],
                    split=policy.get(task_id, group),
                    direction={"wrong_fail": "unfair_fail", "wrong_pass": "wrong_pass"}.get(
                        row["direction"], "other"
                    ),
                )
            )
    return sorted(findings, key=lambda item: item.finding_id)


def build_aba(root: Path, *, state_root: Path | None = None) -> list[Finding]:
    findings = []
    policy = SplitPolicy(root)
    source_root = Path.home() / "Developer/research-context/bridge/2026-10-20-open-qa/_lb"
    for bench, digest in ABA_PINS.items():
        name = bench + "-labels.jsonl"
        data = pinned_bytes(
            "aba-audits",
            digest,
            name,
            digest,
            state_root=state_root,
            local=source_root / name,
        )
        findings.extend(parse_aba(bench, (json.loads(line) for line in data.splitlines()), policy))
    return sorted(findings, key=lambda item: item.finding_id)


COLLECTIONS = {
    "openai-swebench-verified-annotations": build_openai,
    "swebench-pro-verified-corrections": build_pro_corrections,
    "utboost-weak-tasks": build_utboost,
    "patchdiff-wrong-passes": build_patchdiff,
    "aba-audits": build_aba,
}
