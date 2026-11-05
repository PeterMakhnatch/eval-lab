"""Prepare, preregister, execute, and summarize native G3-G6 Gold certification cells for 000682 and 002139.

Reuses frozen ov1106-goldcert hardened packages, frozen mutant pools, score-blind mutant
validity labels, and verified native G1/G2/G7 receipts. Executes native G5/G6 mutant cells
and explicit contained G3/G4 integrity checks through ContainedModalRunner.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from evallab.gold_certifier import (  # noqa: E402
    ContainedModalRunner,
    exit0_pth_script,
    plan_certification_cells,
    wilson_interval,
    witness_script,
)
from evallab.modal_cells import SandboxMeter, compute_cell_key  # noqa: E402
from evallab.mutant_auditor import Mutant  # noqa: E402
from evallab.registry import task_directory_digest  # noqa: E402
from evallab.regrade import CloudSpendGate, SpendCapReached  # noqa: E402

OV1106_PRIVATE = Path("/private/tmp/ov1106/goldcert")
OV1106_PREPARED = OV1106_PRIVATE / "helpers/safe/followup/prepared.json"
OV1106_PREPARED_SHA256 = "ca50ff45823285f22b2995b9723d838d8b0a330c7e9b538298ebe6778f5a8e1b"
OV1106_FOLLOWUP_AGGREGATE = HERE / "followup-aggregate.json"
OV1106_FOLLOWUP_AGGREGATE_SHA256 = (
    "ac5be3db1756af8ec52f6b6b3510df43f67178ddc69150017483ed34a1e775f9"
)
OV1106_MUTANT_VALIDITY = HERE / "mutant-validity.json"
OV1106_MUTANT_VALIDITY_SHA256 = (
    "b2306a8a782f4e94711c48292488e88ae60fb61bc0fc2b40328dc2a8a5f88015"
)
OV1106_MUTANT_PAIRED = HERE / "mutant-paired-replay.json"

LANE = Path("/private/tmp/ev1105/gold")
ROOT = LANE / "execution"
PREPARED = ROOT / "prepared.json"
OUT = ROOT / "execution.json"
FREEZE = HERE / "ev1105-gold-freeze.json"
AGGREGATE = HERE / "ev1105-gold-aggregate.json"

STATE = Path.home() / ".local/state/evallab-ev1105"
LEDGER = STATE / "gold-spend-ledger.json"
SPEND_LOG = STATE / "spend.jsonl"
APP = "ev1105-gold-cert"
BILLING_TAG = "ev1105"
CAP = 4.0
UNIT = 0.02
SELECTED_TASKS = ("000682", "002139")
CONFIG = "/private/tmp/ev1105/shared/nofallback.yml"
APPROVAL = "Peter 2026-11-05 12:55 ET: 'sure do this' (gold slice, about $1-2 Modal)"

CORE = {
    "src/evallab/gold_certifier.py": "2a0523c4bcba81f797cc537841f98071a0bbefa51d40f736f63699c458fd861c",
    "src/evallab/regrade.py": "7659f97d04dc4976f2234dbdfa7a224bb7777ad11eacd8b474627bdb985b624f",
    "src/evallab/modal_cells.py": "c4f49b3cc6831af3d0f69529c9d005e97644a7c8bcc8d50076f66bb2a6350acf",
    "src/evallab/qa/task_modal.py": "96e0ec0f3bf7a4f29916856fe4cb280b0a76b7bbcf8f2832f2ee996b53233955",
}

UNITTEST_RUNNER_SHIMS = {
    "000682": (
        "printf '\\nimport unittest\\n"
        "unittest.TestCase.run = lambda self, result=None: result\\n' >> c7n/resources/sns.py"
    ),
    "002139": (
        "printf '\\nimport unittest\\n"
        "unittest.TestCase.run = lambda self, result=None: result\\n' >> lib/bb/fetch2/__init__.py"
    ),
}

POLICY = {
    "app": APP,
    "approval": APPROVAL,
    "billing_tag": BILLING_TAG,
    "cap_usd": CAP,
    "estimates_not_invoices": True,
    "fixed_reservations_retained": True,
    "gate": "CloudSpendGate",
    "ledger": str(LEDGER),
    "max_concurrency": 4,
    "meter_cap_usd": CAP,
    "native_calls_planned": 16,
    "runner": "ContainedModalRunner",
    "spend_log": str(SPEND_LOG),
    "timeout_seconds": 240,
    "unit_usd": UNIT,
    "units_per_hardened_cell": 6,
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked(path: Path | str, expected: str | None = None) -> bytes:
    resolved = Path(path)
    require(resolved.is_file() and not resolved.is_symlink(), f"nonregular input: {resolved}")
    data = resolved.read_bytes()
    if expected is not None:
        require(sha(data) == expected, f"input drift: {resolved}")
    return data


def load(path: Path | str, expected: str | None = None) -> Any:
    return json.loads(checked(path, expected))


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def binding(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"path": str(resolved), "sha256": sha(checked(resolved))}


def cell_key_for(job: dict[str, Any]) -> str:
    return compute_cell_key(
        package_digest=job["package_digest"],
        variant=job["name"],
        submission_digest=job["script_sha256"],
        backend_image="modal-default",
        grader_hook="",
        timeout_seconds=240,
    )


def runner_and_gate() -> tuple[ContainedModalRunner, CloudSpendGate]:
    os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
    gate = CloudSpendGate(
        approval=APPROVAL,
        cap_usd=CAP,
        spend_ledger=LEDGER,
        usd_per_execution=UNIT,
        modal_app_name=APP,
        billing_tag=BILLING_TAG,
    )
    return ContainedModalRunner(repo_root=REPO, spend_gate=gate, meter=SandboxMeter(CAP)), gate


def validate_native(
    job: dict[str, Any],
    cell: Path,
    hashes: dict[str, str] | None = None,
    *,
    expect_ev1105_cloud: bool = True,
) -> dict[str, Any]:
    names = (
        "agent-script.sh",
        "result.json",
        "trial/qa-execution.json",
        "trial/egress-lock.json",
        "trial/agent/oracle.txt",
        "trial/verifier/test_output.log",
    )
    bindings = {
        name: sha(checked(cell / name, hashes[name] if hashes else None)) for name in names
    }
    checked(cell / "agent-script.sh", job["script_sha256"])
    raw, native, egress = (load(cell / name) for name in names[1:4])
    agent_log = checked(cell / names[4]).decode("utf-8", errors="replace")
    test_output = checked(cell / names[5]).decode("utf-8", errors="replace")
    require(
        raw.get("agent_log") == agent_log
        and raw.get("test_output_tail") == test_output[-600:]
        and raw.get("evidence_sha256") == sha((agent_log + test_output).encode("utf-8")),
        f"native output/evidence digest mismatch: {job['name']}",
    )
    if expect_ev1105_cloud:
        cloud = native.get("cloud_spend") or {}
        require(
            cloud.get("cap_usd") == CAP
            and cloud.get("usd_per_execution") == UNIT
            and cloud.get("modal_app_name") == APP
            and cloud.get("billing_tag") == BILLING_TAG
            and cloud.get("approval") == APPROVAL
            and Path(cloud.get("spend_ledger", "")).resolve() == LEDGER.resolve(),
            f"native cumulative budget identity mismatch: {job['name']}",
        )
    require(
        task_directory_digest(Path(job["package"])) == job["package_digest"],
        f"native package changed: {job['name']}",
    )
    require(
        raw.get("cache_hit") is False
        and raw.get("cell_key") == cell_key_for(job)
        and raw.get("status") == "executed"
        and type(raw.get("reward")) in (int, float)
        and raw["reward"] in (0, 1)
        and native.get("exception") is None
        and native.get("environment") == "modal"
        and native.get("mode") == "shortcut"
        and native.get("package_digest") == job["package_digest"]
        and Path(native["job_path"]).resolve() == (cell / "trial").resolve()
        and native.get("reward") == raw["reward"],
        f"invalid native outcome/identity: {job['name']}",
    )
    marker = job["marker"]
    count = job["script"].count(marker)
    require(
        count > 0
        and raw.get("agent_log", "").count(marker) >= count
        and native.get("agent_stdout", "").count(marker) >= count,
        f"partial application or marker mismatch: {job['name']}",
    )
    require(
        egress.get("schema_version") == 2
        and egress.get("requested") is True
        and egress.get("error") is None
        and any(
            r.get("source") == "creation"
            and r.get("network_mode") in ("public", "allowlist", "no-network")
            and isinstance(r.get("sandbox_id"), str)
            and r["sandbox_id"].startswith("sb-")
            for r in egress.get("applications", [])
        ),
        f"missing actual native creation receipt: {job['name']}",
    )
    return {
        "status": "executed",
        "reward": float(raw["reward"]),
        "hashes": bindings,
        "receipt": str(cell / "result.json"),
        "native_exception": None,
        "est_usd": raw.get("est_usd"),
        "wall_s": raw.get("wall_s"),
        "cell_key": raw["cell_key"],
    }


def load_frozen_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    for rel, expected in CORE.items():
        checked(REPO / rel, expected)
    ov_prepared = load(OV1106_PREPARED, OV1106_PREPARED_SHA256)
    ov_aggregate = load(OV1106_FOLLOWUP_AGGREGATE, OV1106_FOLLOWUP_AGGREGATE_SHA256)
    validity = load(OV1106_MUTANT_VALIDITY)
    paired = load(OV1106_MUTANT_PAIRED)

    # Map (task_id, control_name) -> static category from the blind audit
    categories: dict[tuple[str, str], dict[str, Any]] = {}
    for item in validity["controls"]:
        tid = item["task_id"]
        cname = item["control_name"]
        categories[tid, cname] = {
            "category": item["category"],
            "split": item["split"],
            "control_sha256": item["control_sha256"],
            "script_sha256": item["script_sha256"],
            "mutation_delta_sha256": item["mutation_delta_sha256"],
            "necessarily_violates_public_contract": item["necessarily_violates_public_contract"],
            "rationale": item["rationale"],
        }
    paired_by_key = {(r["task"], r["control"]): r for r in paired["rows"]}
    require(
        validity["verification"]["classification_counts"]
        == {"DEFECT": 11, "POSSIBLY_VALID": 7, "UNCERTAIN": 0}
        and paired["counts"]["DEFECT"]["paired_rows"] == 10
        and paired["counts"]["POSSIBLY_VALID"]["paired_rows"] == 6,
        "blind audit mutant counts changed",
    )

    # Verify hardened packages for 000682 and 002139 on disk
    packages: dict[str, Any] = {}
    for tid in SELECTED_TASKS:
        pkg_info = ov_prepared["packages"][tid]
        pkg_path = Path(pkg_info["package"])
        require(
            task_directory_digest(pkg_path) == pkg_info["package_digest"],
            f"hardened package digest drift for {tid}",
        )
        packages[tid] = pkg_info

    # Verify existing native G1, G2, G7 receipts for 000682 and 002139
    ov_jobs_by_name = {j["name"]: j for j in ov_prepared["jobs"]}
    reused_rows: list[dict[str, Any]] = []
    for row in ov_aggregate["rows"]:
        if row["task"] not in SELECTED_TASKS or row["arm"] not in ("reference", "nop", "alternative"):
            continue
        require(row["status"] == "executed" and row["reward"] in (0.0, 1.0), f"missing prior G1/G2/G7 row: {row['name']}")
        ov_job = ov_jobs_by_name[row["name"]]
        cell_dir = Path(row["receipt"]).parent
        check_job = dict(ov_job, name=cell_dir.name)
        proof = validate_native(check_job, cell_dir, row["hashes"], expect_ev1105_cloud=False)
        require(proof["reward"] == float(row["reward"]), f"reused reward drift: {row['name']}")
        reused_rows.append(
            {
                "name": row["name"],
                "task": row["task"],
                "arm": row["arm"],
                "control": row["control"],
                "split": row.get("split"),
                "alternative_informed": row.get("alternative_informed", False),
                "package": row["package"],
                "package_digest": row["package_digest"],
                "contract_sha256": row["contract_sha256"],
                "script_sha256": row["script_sha256"],
                **proof,
            }
        )
    require(len(reused_rows) == 12, "expected 12 verified native G1/G2/G7 rows across 000682 and 002139")

    # Build the 16 new native jobs (10 frozen mutants + 6 explicit contained G3/G4 integrity checks)
    new_jobs: list[dict[str, Any]] = []
    dummy_gate = CloudSpendGate(
        approval=APPROVAL,
        cap_usd=CAP,
        usd_per_execution=UNIT,
        modal_app_name=APP,
        billing_tag=BILLING_TAG,
    )
    dummy_runner = ContainedModalRunner(repo_root=REPO, spend_gate=dummy_gate, meter=SandboxMeter(CAP))

    for tid in SELECTED_TASKS:
        pkg_info = packages[tid]
        pkg_path = Path(pkg_info["package"])
        workdir = pkg_info["hardening"]["workdir"]

        # 1. Explicit contained G3/G4 integrity cells via gold_certifier.plan_certification_cells
        planned_cells = plan_certification_cells(
            package=pkg_path,
            upgraded=pkg_path,
            reference_patch="--- a/x.py\n+++ b/x.py\n",
            train=[Mutant("a.py", "op", 1, "d", "s", "diff", "0001")],
            evals=[Mutant("b.py", "op", 2, "d", "s", "diff", "0002")],
            alternatives=[],
            witnesses=[None, UNITTEST_RUNNER_SHIMS[tid]],
            injection_controls=True,
            run_cell=dummy_runner,
        )
        integrity_cells = [c for c in planned_cells if c.arm in ("witness", "exit0_pth")]
        require(
            [c.name for c in integrity_cells] == ["witness-0", "witness-1", "exit0_pth"],
            f"unexpected integrity cells for {tid}",
        )
        require(
            integrity_cells[0].script == witness_script(workdir, None)
            and integrity_cells[1].script == witness_script(workdir, UNITTEST_RUNNER_SHIMS[tid])
            and integrity_cells[2].script == exit0_pth_script(workdir),
            f"integrity script mismatch for {tid}",
        )
        for cell in integrity_cells:
            kind_desc = (
                "conftest_pytest_sessionfinish_exit0"
                if cell.name == "witness-0"
                else "unittest_testcase_run_noop_injection"
                if cell.name == "witness-1"
                else "site_packages_pth_os_exit0"
            )
            new_jobs.append(
                {
                    "name": f"{tid}-ev1105-{cell.name}",
                    "task": tid,
                    "arm": "witness" if cell.arm == "witness" else "startup",
                    "certifier_arm": cell.arm,
                    "control": cell.name,
                    "integrity_kind": kind_desc,
                    "package": str(pkg_path),
                    "package_digest": pkg_info["package_digest"],
                    "contract_sha256": pkg_info["contract_sha256"],
                    "script": cell.script,
                    "script_sha256": sha(cell.script.encode("utf-8")),
                    "marker": cell.marker,
                    "mutant_sha256": None,
                    "static_category": None,
                }
            )

        # 2. Frozen G5 (train) and G6 (eval) mutant cells from ov1106-goldcert
        task_mutant_jobs = [
            j for j in ov_prepared["jobs"] if j["task"] == tid and j["arm"] in ("train", "eval")
        ]
        for mjob in task_mutant_jobs:
            cname = mjob["control"]
            cat_info = categories[tid, cname]
            paired_info = paired_by_key[tid, cname]
            require(
                mjob["script_sha256"] == cat_info["script_sha256"]
                and sha(mjob["script"].encode("utf-8")) == mjob["script_sha256"]
                and mjob["mutant_sha256"] == cat_info["control_sha256"]
                and mjob["arm"] == cat_info["split"] == paired_info["split"]
                and cat_info["category"] == paired_info["static_category"],
                f"frozen mutant metadata mismatch: {tid} {cname}",
            )
            new_jobs.append(
                {
                    "name": f"{tid}-ev1105-{cname}",
                    "historical_followup_name": mjob["name"],
                    "task": tid,
                    "arm": mjob["arm"],
                    "certifier_arm": mjob["arm"],
                    "control": cname,
                    "package": str(pkg_path),
                    "package_digest": pkg_info["package_digest"],
                    "contract_sha256": pkg_info["contract_sha256"],
                    "script": mjob["script"],
                    "script_sha256": mjob["script_sha256"],
                    "marker": mjob["marker"],
                    "mutant_sha256": mjob["mutant_sha256"],
                    "mutation_delta_sha256": cat_info["mutation_delta_sha256"],
                    "static_category": cat_info["category"],
                    "static_rationale": cat_info["rationale"],
                    "standalone_original_reward": paired_info["original_reward"],
                    "standalone_public_reward": paired_info["public_reward"],
                }
            )

    require(
        len(new_jobs) == 16
        and sum(j["arm"] in ("train", "eval") for j in new_jobs) == 10
        and sum(j["static_category"] == "DEFECT" for j in new_jobs) == 7
        and sum(j["static_category"] == "POSSIBLY_VALID" for j in new_jobs) == 3
        and sum(j["arm"] in ("witness", "startup") for j in new_jobs) == 6,
        "unexpected ev1105-gold job counts",
    )
    return packages, ov_aggregate, {"reused_rows": reused_rows}, new_jobs


def prepare() -> None:
    require(
        not any(p.exists() for p in (ROOT, FREEZE, AGGREGATE, LEDGER)),
        "fresh ev1105-gold preparation required",
    )
    packages, _, reused, new_jobs = load_frozen_inputs()
    ROOT.mkdir(parents=True, exist_ok=False)

    inputs = [
        OV1106_PREPARED,
        OV1106_FOLLOWUP_AGGREGATE,
        OV1106_MUTANT_VALIDITY,
        OV1106_MUTANT_PAIRED,
    ]
    sources = [
        HERE / "measure_ev1105_gold.py",
        HERE / "followup-aggregate.json",
        HERE / "mutant-validity.json",
        HERE / "mutant-paired-replay.json",
        HERE / "contracts-v3/000682.py",
        HERE / "contracts/002139.py",
        *(REPO / rel for rel in CORE),
    ]
    manifest = {
        "schema": "ev1105-gold-prepared-v1",
        "prepared_at": datetime.now(UTC).isoformat(),
        "policy": POLICY,
        "selected_tasks": list(SELECTED_TASKS),
        "packages": {
            tid: {k: v for k, v in p.items() if k != "hardening"} for tid, p in packages.items()
        },
        "reused_G1_G2_G7": reused["reused_rows"],
        "jobs": new_jobs,
        "inputs": [binding(p) for p in inputs],
        "source_files": [binding(p) for p in sources],
        "preregistered_rules": {
            "G1_reference": "reward == 1.0 on hardened native verifier",
            "G2_nop": "reward == 0.0 on hardened native verifier",
            "G3_witness": "all explicit contained witness cells (conftest pytest_sessionfinish exit-0 shim and unittest TestCase.run injection) must score reward == 0.0",
            "G4_startup": "explicit contained exit0_pth (.pth os._exit(0) startup hook in site-packages) must score reward == 0.0",
            "G5_G6_mutants": "DEFECT mutants (fixed by score-blind audit before scores) must be rejected (reward == 0.0); POSSIBLY_VALID mutants are reported separately and not counted for or against certification",
            "G7_alternatives": "all 4 blind source-only candidate solutions must be accepted (reward == 1.0)",
        },
    }
    save(PREPARED, manifest)
    freeze_doc = {
        "schema": "ev1105-gold-freeze-v1",
        "prepared": binding(PREPARED),
        "policy": POLICY,
        "selected_tasks": list(SELECTED_TASKS),
        "planned_jobs": [
            {k: v for k, v in j.items() if k != "script"} for j in new_jobs
        ],
        "source_files": manifest["source_files"],
        "preregistered_rules": manifest["preregistered_rules"],
    }
    save(FREEZE, freeze_doc)
    PREPARED.chmod(0o444)
    print(
        json.dumps(
            {
                "freeze": binding(FREEZE),
                "prepared": binding(PREPARED),
                "native_calls_planned": len(new_jobs),
                "policy": POLICY,
            },
            sort_keys=True,
        )
    )


def verify_frozen(expected_freeze_sha: str) -> dict[str, Any]:
    require(
        re.fullmatch(r"[0-9a-f]{64}", expected_freeze_sha or "") is not None,
        "exact 64-hex freeze SHA256 required",
    )
    freeze = load(FREEZE, expected_freeze_sha)
    require(
        freeze["prepared"]["path"] == str(PREPARED.resolve()) and freeze["policy"] == POLICY,
        "freeze identity changed",
    )
    manifest = load(PREPARED, freeze["prepared"]["sha256"])
    require(
        manifest["policy"] == POLICY and manifest["source_files"] == freeze["source_files"],
        "policy/source drift",
    )
    for item in manifest["inputs"] + manifest["source_files"]:
        checked(item["path"], item["sha256"])
    for tid in SELECTED_TASKS:
        pkg = manifest["packages"][tid]
        require(
            task_directory_digest(Path(pkg["package"])) == pkg["package_digest"],
            f"hardened package drift for {tid}",
        )
    return manifest


def verify_publication(commit: str, manifest: dict[str, Any]) -> None:
    require(
        re.fullmatch(r"[0-9a-f]{40}", commit or "") is not None,
        "full 40-hex publication commit required",
    )
    files = [FREEZE] + [Path(item["path"]) for item in manifest["source_files"]]
    relative = list(dict.fromkeys(p.relative_to(REPO).as_posix() for p in files))
    archive = subprocess.run(
        ["git", "archive", commit, "--", *relative],
        cwd=REPO,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for path in relative:
            member = tree.getmember(path)
            require(member.isfile(), f"published file is not regular: {path}")
            stream = tree.extractfile(member)
            require(
                stream is not None and stream.read() == checked(REPO / path),
                f"unpublished frozen bytes: {path}",
            )
    remote = subprocess.run(
        ["git", "ls-remote", "--heads", "origin"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    require(
        any(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, line.split()[0]],
                cwd=REPO,
                capture_output=True,
            ).returncode
            == 0
            for line in remote.stdout.splitlines()
            if line.strip()
        ),
        "commit absent from published origin branches",
    )


def log_spend(
    job: dict[str, Any], phase: str, estimate: float | None, actual_usd: float | None
) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "lane": "gold",
        "phase": phase,
        "what": f"{phase}:ev1105-gold:{job['name']}",
        "provider": "modal",
        "est_usd": estimate,
        "actual_usd": actual_usd,
        "receipt": str(ROOT / "cells" / job["name"] / "result.json"),
    }
    with SPEND_LOG.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_fresh_cell(job: dict[str, Any]) -> None:
    cell = ROOT / "cells" / job["name"]
    require(
        not cell.exists() and not (cell.parent / cell_key_for(job)).exists(),
        f"fresh cell/cache path required: {job['name']}",
    )


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    ensure_fresh_cell(job)
    require(
        task_directory_digest(Path(job["package"])) == job["package_digest"],
        f"package drift before allocation: {job['name']}",
    )
    cell = ROOT / "cells" / job["name"]
    cell.mkdir(parents=True, exist_ok=False)
    (cell / "agent-script.sh").write_bytes(job["script"].encode("utf-8"))
    row: dict[str, Any] = {
        "name": job["name"],
        "task": job["task"],
        "arm": job["arm"],
        "control": job["control"],
        "static_category": job.get("static_category"),
        "status": "unmeasured",
        "reward": None,
        "reason": None,
        "script_sha256": job["script_sha256"],
        "package_digest": job["package_digest"],
    }
    reserved_estimate = round(6 * UNIT, 5)
    log_spend(job, "before", reserved_estimate, None)
    try:
        native_runner, gate = runner_and_gate()
        native_runner(cell, Path(job["package"]), job["script"])
        row.update(validate_native(job, cell))
        row["fixed_reservations"] = gate.to_json()
    except Exception as exc:
        row.update(
            status="unmeasured",
            reward=None,
            reason=f"{type(exc).__name__}: {exc}",
        )
        (cell / "exception.txt").write_text(traceback.format_exc(), encoding="utf-8")
        row["hashes"] = {
            name: sha(checked(cell / name))
            for name in (
                "agent-script.sh",
                "result.json",
                "trial/qa-execution.json",
                "trial/egress-lock.json",
                "trial/agent/oracle.txt",
                "trial/verifier/test_output.log",
            )
            if (cell / name).is_file()
        }
        row["receipt"] = str(cell / "result.json")
        native_path = cell / "trial/qa-execution.json"
        if native_path.is_file():
            row["native_exception"] = load(native_path).get("exception")
        raw_path = cell / "result.json"
        detail = (
            f"{exc} {row.get('native_exception')} "
            f"{load(raw_path).get('reason') if raw_path.is_file() else ''}"
        )
        row["cap_reached"] = (
            isinstance(exc, SpendCapReached)
            or "SpendCapReached" in detail
            or "remaining persisted campaign cap" in detail
            or "Modal cap $" in detail
        )
    finally:
        actual = row.get("est_usd")
        log_spend(job, "after", reserved_estimate, actual if isinstance(actual, int | float) else 0.0)
        save(cell / "measurement.json", row)
    return row


def active_owned_apps() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["modal", "app", "list", "--json"], check=True, capture_output=True, text=True
    )
    return [
        r
        for r in json.loads(result.stdout)
        if r.get("description") == APP and r.get("state") != "stopped"
    ]


def execute(expected_freeze_sha: str, commit: str) -> None:
    manifest = verify_frozen(expected_freeze_sha)
    verify_publication(commit, manifest)
    require(
        not OUT.exists() and not (ROOT / "cells").exists() and not LEDGER.exists(),
        "execution namespace or ledger already exists",
    )
    require(not active_owned_apps(), "owned Modal app already active; refusing")
    jobs = manifest["jobs"]
    require(len(jobs) == 16, "expected 16 planned jobs")
    for job in jobs:
        ensure_fresh_cell(job)

    report: dict[str, Any] = {
        "schema": "ev1105-gold-execution-v1",
        "publication_commit": commit,
        "freeze_sha256": expected_freeze_sha,
        "policy": POLICY,
        "rows": [],
        "started_at": datetime.now(UTC).isoformat(),
    }
    save(OUT, report)
    try:
        with ThreadPoolExecutor(max_workers=POLICY["max_concurrency"]) as pool:
            for offset in range(0, len(jobs), POLICY["max_concurrency"]):
                batch = list(pool.map(run_job, jobs[offset : offset + POLICY["max_concurrency"]]))
                report["rows"].extend(batch)
                save(OUT, report)
                require(
                    not any(r.get("cap_reached") for r in batch),
                    "native spend cap reached; stopping further allocations",
                )
    except Exception as exc:
        report["stop_reason"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        stops = []
        try:
            for app in active_owned_apps():
                stopped = subprocess.run(
                    ["modal", "app", "stop", app["app_id"]],
                    capture_output=True,
                    text=True,
                )
                stops.append(
                    {
                        "app_id": app["app_id"],
                        "returncode": stopped.returncode,
                        "stdout": stopped.stdout,
                        "stderr": stopped.stderr,
                    }
                )
            report["remaining_owned_apps"] = active_owned_apps()
        except Exception as exc:
            report["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        report["app_stops"] = stops
        report["ledger"] = binding(LEDGER) if LEDGER.is_file() else None
        report["ledger_snapshot"] = load(LEDGER) if LEDGER.is_file() else None
        report["finished_at"] = datetime.now(UTC).isoformat()
        save(OUT, report)


def stat_counts(rows: list[dict[str, Any]], wanted: float) -> dict[str, Any]:
    measured = [r for r in rows if r.get("reward") is not None]
    success = sum(float(r["reward"]) == wanted for r in measured)
    return {
        "planned": len(rows),
        "measured": len(measured),
        "accepted": sum(float(r["reward"]) == 1.0 for r in measured),
        "rejected": sum(float(r["reward"]) == 0.0 for r in measured),
        "unmeasured": len(rows) - len(measured),
        "successes": success,
        "wilson95": list(wilson_interval(success, len(measured))) if measured else None,
    }


def summarize(expected_freeze_sha: str, commit: str) -> None:
    manifest = verify_frozen(expected_freeze_sha)
    verify_publication(commit, manifest)
    require(
        not AGGREGATE.exists() and not (ROOT / "aggregate.json").exists(),
        "aggregate already exists",
    )
    execution = load(OUT)
    require(
        execution["publication_commit"] == commit
        and execution["freeze_sha256"] == expected_freeze_sha
        and execution["policy"] == POLICY,
        "execution publication mismatch",
    )
    actual_by_name = {r["name"]: r for r in execution["rows"]}
    require(len(actual_by_name) == len(execution["rows"]) == 16, "expected 16 executed rows")

    executed_rows: list[dict[str, Any]] = []
    for job in manifest["jobs"]:
        saved = actual_by_name[job["name"]]
        cell = ROOT / "cells" / job["name"]
        require(load(cell / "measurement.json") == saved, f"measurement drift: {job['name']}")
        require(
            saved["script_sha256"] == job["script_sha256"]
            and saved["package_digest"] == job["package_digest"],
            f"measurement identity drift: {job['name']}",
        )
        for name, digest_val in saved.get("hashes", {}).items():
            checked(cell / name, digest_val)
        row = {k: v for k, v in job.items() if k not in ("script", "marker")}
        row.update(saved)
        if saved["status"] == "executed":
            proof = validate_native(job, cell, saved["hashes"])
            require(proof["reward"] == saved["reward"], f"reward drift: {job['name']}")
            row.update(proof)
        else:
            require(saved["reward"] is None, "failed cell cannot carry a reward")
        executed_rows.append(row)

    all_task_rows = list(manifest["reused_G1_G2_G7"]) + executed_rows
    tasks_summary: dict[str, Any] = {}
    for tid in SELECTED_TASKS:
        trows = [r for r in all_task_rows if r["task"] == tid]
        ref_rows = [r for r in trows if r["arm"] == "reference"]
        nop_rows = [r for r in trows if r["arm"] == "nop"]
        wit_rows = [r for r in trows if r["arm"] == "witness"]
        pth_rows = [r for r in trows if r["arm"] == "startup"]
        train_all = [r for r in trows if r["arm"] == "train"]
        eval_all = [r for r in trows if r["arm"] == "eval"]
        train_defect = [r for r in train_all if r.get("static_category") == "DEFECT"]
        train_pv = [r for r in train_all if r.get("static_category") == "POSSIBLY_VALID"]
        eval_defect = [r for r in eval_all if r.get("static_category") == "DEFECT"]
        eval_pv = [r for r in eval_all if r.get("static_category") == "POSSIBLY_VALID"]
        defect_all = train_defect + eval_defect
        pv_all = train_pv + eval_pv
        alt_rows = [r for r in trows if r["arm"] == "alternative"]

        g1_pass = bool(ref_rows) and all(r["reward"] == 1.0 for r in ref_rows)
        g2_pass = bool(nop_rows) and all(r["reward"] == 0.0 for r in nop_rows)
        g3_pass = bool(wit_rows) and all(r["reward"] == 0.0 for r in wit_rows)
        g4_pass = bool(pth_rows) and all(r["reward"] == 0.0 for r in pth_rows)
        g7_pass = bool(alt_rows) and all(r["reward"] == 1.0 for r in alt_rows)

        # G5 / G6 under blind-audit DEFECT rule:
        # 1) vacuous-split rule: no DEFECT mutant in the split is accepted (and all are measured)
        g5_defect_no_survivor = all(r["reward"] == 0.0 for r in train_defect) and all(
            r["reward"] is not None for r in train_all
        )
        # 2) non-empty split rule: requires >= 1 DEFECT mutant in the split and all rejected
        g5_defect_nonempty = bool(train_defect) and all(r["reward"] == 0.0 for r in train_defect)
        g6_defect_pass = bool(eval_defect) and all(r["reward"] == 0.0 for r in eval_defect)
        pooled_defect_pass = bool(defect_all) and all(r["reward"] == 0.0 for r in defect_all)

        # Kill-all raw rule (if POSSIBLY_VALID were counted against certification):
        g5_raw_kill_all = bool(train_all) and all(r["reward"] == 0.0 for r in train_all)
        g6_raw_kill_all = bool(eval_all) and all(r["reward"] == 0.0 for r in eval_all)

        certified_defect_rule = (
            g1_pass
            and g2_pass
            and g3_pass
            and g4_pass
            and g5_defect_no_survivor
            and g6_defect_pass
            and pooled_defect_pass
            and g7_pass
        )
        certified_strict_nonempty_split_rule = (
            g1_pass
            and g2_pass
            and g3_pass
            and g4_pass
            and g5_defect_nonempty
            and g6_defect_pass
            and g7_pass
            and (tid != "000682")
        )

        tasks_summary[tid] = {
            "package": manifest["packages"][tid]["package"],
            "package_digest": manifest["packages"][tid]["package_digest"],
            "contract_sha256": manifest["packages"][tid]["contract_sha256"],
            "alternative_informed": tid == "000682",
            "newly_held_out": tid != "000682",
            "gates": {
                "G1": g1_pass,
                "G2": g2_pass,
                "G3": g3_pass,
                "G4": g4_pass,
                "G5_defect_no_survivor": g5_defect_no_survivor,
                "G5_defect_nonempty": g5_defect_nonempty,
                "G5_raw_kill_all": g5_raw_kill_all,
                "G6_defect": g6_defect_pass,
                "G6_raw_kill_all": g6_raw_kill_all,
                "G5_G6_pooled_defect": pooled_defect_pass,
                "G7": g7_pass,
            },
            "certified": certified_defect_rule,
            "certified_strict_nonempty_g5_uninformed_g7": certified_strict_nonempty_split_rule,
            "counts": {
                "reference": stat_counts(ref_rows, 1.0),
                "nop": stat_counts(nop_rows, 0.0),
                "witness": stat_counts(wit_rows, 0.0),
                "startup": stat_counts(pth_rows, 0.0),
                "train_defect": stat_counts(train_defect, 0.0),
                "train_possibly_valid": stat_counts(train_pv, 0.0),
                "train_all": stat_counts(train_all, 0.0),
                "eval_defect": stat_counts(eval_defect, 0.0),
                "eval_possibly_valid": stat_counts(eval_pv, 0.0),
                "eval_all": stat_counts(eval_all, 0.0),
                "defect_all": stat_counts(defect_all, 0.0),
                "possibly_valid_all": stat_counts(pv_all, 0.0),
                "alternative": stat_counts(alt_rows, 1.0),
            },
        }

    ledger = load(LEDGER)
    require(
        ledger == execution["ledger_snapshot"]
        and Decimal(str(ledger["reserved_usd"])) <= Decimal(str(CAP))
        and len(ledger["reservations"]) == 96,
        "expected 96 fixed reservations (16 cells x 6 units) within $4.00 cap",
    )
    actual_meter_usd = round(
        sum(float(r.get("est_usd") or 0.0) for r in executed_rows), 5
    )

    all_defect = [r for r in executed_rows if r.get("static_category") == "DEFECT"]
    all_pv = [r for r in executed_rows if r.get("static_category") == "POSSIBLY_VALID"]
    all_integrity = [r for r in executed_rows if r["arm"] in ("witness", "startup")]

    aggregate = {
        "schema": "ev1105-gold-aggregate-v1",
        "publication_commit": commit,
        "freeze_sha256": expected_freeze_sha,
        "policy": POLICY,
        "prepared": binding(PREPARED),
        "execution": binding(OUT),
        "tasks": tasks_summary,
        "pooled_counts": {
            "defect_mutants": stat_counts(all_defect, 0.0),
            "possibly_valid_mutants": stat_counts(all_pv, 0.0),
            "all_mutants": stat_counts(all_defect + all_pv, 0.0),
            "integrity_controls": stat_counts(all_integrity, 0.0),
            "witness_controls": stat_counts(
                [r for r in all_integrity if r["arm"] == "witness"], 0.0
            ),
            "startup_controls": stat_counts(
                [r for r in all_integrity if r["arm"] == "startup"], 0.0
            ),
            "new_native_cells": stat_counts(executed_rows, 0.0),
        },
        "spend": {
            "cap_usd": CAP,
            "unit_usd": UNIT,
            "reservations_count": len(ledger["reservations"]),
            "reserved_usd": ledger["reserved_usd"],
            "measured_meter_usd": actual_meter_usd,
            "ledger_path": str(LEDGER),
            "spend_log_path": str(SPEND_LOG),
            "app_stops": execution.get("app_stops", []),
            "remaining_owned_apps": execution.get("remaining_owned_apps", []),
        },
        "reused_G1_G2_G7_rows": manifest["reused_G1_G2_G7"],
        "executed_rows": executed_rows,
    }
    save(ROOT / "aggregate.json", aggregate)
    save(AGGREGATE, aggregate)
    print(
        json.dumps(
            {
                "tasks": {
                    tid: {
                        "gates": t["gates"],
                        "certified": t["certified"],
                        "certified_strict_nonempty_g5_uninformed_g7": t[
                            "certified_strict_nonempty_g5_uninformed_g7"
                        ],
                    }
                    for tid, t in tasks_summary.items()
                },
                "pooled_counts": aggregate["pooled_counts"],
                "spend": aggregate["spend"],
            },
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "execute", "summarize"))
    parser.add_argument("--freeze-sha256")
    parser.add_argument("--published-commit")
    args = parser.parse_args()
    os.environ["PI_CONFIG_FILES"] = CONFIG
    if args.stage == "prepare":
        require(
            args.freeze_sha256 is None and args.published_commit is None,
            "publication flags belong to execute/summarize",
        )
        prepare()
    else:
        require(ROOT.is_dir(), "run prepare and publish first")
        require(
            args.freeze_sha256 is not None and args.published_commit is not None,
            "--freeze-sha256 and --published-commit are required",
        )
        with (ROOT / "controller.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.stage == "execute":
                execute(args.freeze_sha256, args.published_commit)
            else:
                summarize(args.freeze_sha256, args.published_commit)


if __name__ == "__main__":
    main()
