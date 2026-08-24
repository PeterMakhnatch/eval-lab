"""Focused unit tests for SEQGEN v0 generator.

Covers acceptance criteria a through h:
a. Determinism: identical args+now => byte-identical tree.
b. Validity: replaying recorded sequences satisfies all preconditions and ends non-empty.
c. Simulator / RP equivalence: executing solve.sh rp commands yields expected.jsonl.
d. Workbench static acceptance: inspect_candidate passes static checks.
e. Leakage prevention: no solve.sh lines in instruction.md, no test/golden leak in environment/.
f. Adversarial wrongness: plausible-wrong.sh payload != expected.jsonl rows.
g. Coverage correctness: BATCH.json bigrams match recomputed set; first pick is greedy-optimal.
h. Provenance integrity: provenance.json validates against ProvenanceMetadata; digest matches.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evallab.schemas import ProvenanceMetadata
from evallab.seqgen import (
    DOMAIN_SPEC,
    apply_op_to_state,
    compute_package_manifest_digest,
    enumerate_valid_ops,
    extract_bigrams,
    generate_batch,
    generate_candidate_pool,
    select_candidates_by_coverage,
)
from evallab.task_workbench import CandidateSource, inspect_candidate


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def sample_batch(tmp_path: Path) -> tuple[Path, dict]:
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    out_dir = tmp_path / "batch_sample"
    batch = generate_batch(
        seed=7,
        count=3,
        pool=40,
        out_dir=out_dir,
        now=now,
    )
    return out_dir, batch


def test_a_determinism(tmp_path: Path) -> None:
    """Two generate_batch runs with same args+now yield identical files and sha256."""
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    dir1 = tmp_path / "run1" / "test_batch"
    dir2 = tmp_path / "run2" / "test_batch"
    batch1 = generate_batch(seed=42, count=3, pool=30, out_dir=dir1, now=now)
    batch2 = generate_batch(seed=42, count=3, pool=30, out_dir=dir2, now=now)

    assert batch1 == batch2

    files1 = {
        p.relative_to(dir1).as_posix(): _file_digest(p)
        for p in dir1.rglob("*")
        if p.is_file()
    }
    files2 = {
        p.relative_to(dir2).as_posix(): _file_digest(p)
        for p in dir2.rglob("*")
        if p.is_file()
    }

    assert files1 == files2


def test_b_validity(sample_batch: tuple[Path, dict]) -> None:
    """For every generated task, replaying the sequence passes every precondition."""
    batch_dir, batch = sample_batch
    for task_info in batch["tasks"]:
        slug = task_info["slug"]
        task_dir = batch_dir / slug
        gen_record = json.loads((task_dir / "generation.json").read_text(encoding="utf-8"))

        dataset_lines = (
            (task_dir / "environment/orders.jsonl").read_text(encoding="utf-8").splitlines()
        )
        dataset = [json.loads(line) for line in dataset_lines if line.strip()]

        current_schema = dict(DOMAIN_SPEC["initial_schema"])
        current_rows = dataset
        prev_op_args: tuple[str, str] | None = None

        sequence = gen_record["sequence"]
        assert 3 <= len(sequence) <= 6

        for step in sequence:
            op = step["op"]
            args = step["args"]

            # Check op was in valid enumerated ops at this state
            valid_ops = enumerate_valid_ops(current_schema, current_rows, prev_op_args)
            matching = [
                v for v in valid_ops if v["op"] == op and v["args"] == args
            ]
            assert len(matching) == 1, f"Op {op} with {args} was not valid at state"

            current_schema, current_rows = apply_op_to_state(
                current_schema, current_rows, op, args
            )
            prev_op_args = (op, json.dumps(args, sort_keys=True))

        assert len(current_rows) > 0
        expected_lines = (
            (task_dir / "tests/fixtures/expected.jsonl").read_text(encoding="utf-8").splitlines()
        )
        expected_rows = [json.loads(line) for line in expected_lines if line.strip()]
        assert current_rows == expected_rows


def test_c_simulator_rp_equivalence(sample_batch: tuple[Path, dict], tmp_path: Path) -> None:
    """Execute solution/solve.sh commands as real subprocesses against environment/orders.jsonl."""
    batch_dir, batch = sample_batch
    first_task = batch["tasks"][0]["slug"]
    task_dir = batch_dir / first_task

    orders_path = task_dir / "environment/orders.jsonl"
    rp_script = task_dir / "environment/rp"
    expected_bytes = (task_dir / "tests/fixtures/expected.jsonl").read_bytes()

    # Parse solve.sh lines
    solve_lines = (task_dir / "solution/solve.sh").read_text(encoding="utf-8").splitlines()
    work_dir = tmp_path / "subproc_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    out_file = work_dir / "result.jsonl"

    for line in solve_lines:
        line = line.strip()
        if not line.startswith("/app/bin/rp"):
            continue

        # Replace /app/bin/rp with python sys.executable rp_script
        # Replace /app/data/orders.jsonl with orders_path
        # Replace /app/output/result.jsonl or "$OUTPUT" with out_file
        # Replace /tmp/ with work_dir/
        parts = line.split()
        cmd = [sys.executable, str(rp_script)]
        i = 1
        while i < len(parts):
            part = parts[i]
            if part == '"$INPUT"' or part == "/app/data/orders.jsonl":
                cmd.append(str(orders_path))
            elif part == '"$OUTPUT"' or part == "/app/output/result.jsonl":
                cmd.append(str(out_file))
            elif part.startswith("/tmp/"):
                sub_name = Path(part).name
                cmd.append(str(work_dir / sub_name))
            else:
                cmd.append(part)
            i += 1

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"Command failed: {cmd}\nstderr: {result.stderr}"

    assert out_file.is_file()
    assert out_file.read_bytes() == expected_bytes


def test_d_workbench_static_acceptance(sample_batch: tuple[Path, dict], tmp_path: Path) -> None:
    """inspect_candidate passes static checks for generated packages."""
    batch_dir, batch = sample_batch

    # Create a mock repo root layout containing the task package
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    task_slug = batch["tasks"][0]["slug"]
    task_src = batch_dir / task_slug
    task_dest = repo_root / "library/synthetic" / batch_dir.name / task_slug
    task_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(task_src, task_dest)

    source = CandidateSource(
        source_uri=f"library/synthetic/{batch_dir.name}/{task_slug}",
        source_ref="seqgen@0.1.0",
        license="MIT",
        provenance_zone="03-synthetic",
    )

    inspection = inspect_candidate(
        repo_root=repo_root,
        task_path=Path("library/synthetic") / batch_dir.name / task_slug,
        source=source,
    )

    assert inspection.static_passed is True, f"Diagnostics: {inspection.diagnostics}"
    assert len([d for d in inspection.diagnostics if d.severity == "error"]) == 0


def test_e_leakage_prevention(sample_batch: tuple[Path, dict]) -> None:
    """No line of solve.sh appears in instruction.md; environment/ has no hidden verifier files."""
    batch_dir, batch = sample_batch
    for task_info in batch["tasks"]:
        slug = task_info["slug"]
        task_dir = batch_dir / slug

        instruction_text = (task_dir / "instruction.md").read_text(encoding="utf-8")
        solve_lines = (task_dir / "solution/solve.sh").read_text(encoding="utf-8").splitlines()

        for line in solve_lines:
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or stripped in ("set -eu", "mkdir -p /app/output")
            ):
                continue
            assert stripped not in instruction_text, f"Leaked solve line: {stripped}"

        # Verify environment/ only contains orders.jsonl, Dockerfile, rp
        env_files = {p.name for p in (task_dir / "environment").iterdir()}
        assert env_files == {"Dockerfile", "orders.jsonl", "rp"}

        # Verify expected.jsonl exists ONLY under tests/fixtures/
        all_expected = [
            p.relative_to(task_dir).as_posix()
            for p in task_dir.rglob("expected.jsonl")
        ]
        assert all_expected == ["tests/fixtures/expected.jsonl"]


def test_f_adversarial_wrongness(sample_batch: tuple[Path, dict]) -> None:
    """plausible-wrong.sh writes rows differing from expected.jsonl for every task."""
    batch_dir, batch = sample_batch
    for task_info in batch["tasks"]:
        slug = task_info["slug"]
        task_dir = batch_dir / slug

        expected_lines = (
            (task_dir / "tests/fixtures/expected.jsonl").read_text(encoding="utf-8").splitlines()
        )
        expected_rows = [json.loads(line) for line in expected_lines if line.strip()]

        plausible_sh = (
            task_dir / "workbench/adversarial/plausible-wrong.sh"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"cat << 'EOF' > /app/output/result\.jsonl\n(.*?)EOF", plausible_sh, re.DOTALL
        )
        assert match is not None, "Could not find payload in plausible-wrong.sh"
        payload_lines = match.group(1).strip().splitlines()
        payload_rows = [json.loads(line) for line in payload_lines if line.strip()]

        assert payload_rows != expected_rows
        assert len(payload_rows) > 0


def test_g_coverage_correctness(sample_batch: tuple[Path, dict]) -> None:
    """BATCH.json bigram set equals recomputation and first selection is greedy-optimal."""
    batch_dir, batch = sample_batch
    tasks_info = batch["tasks"]

    # Recompute bigrams from selected task generation records
    recomputed_bigrams: set[str] = set()
    for task_info in tasks_info:
        task_dir = batch_dir / task_info["slug"]
        gen_record = json.loads((task_dir / "generation.json").read_text(encoding="utf-8"))
        seq = gen_record["sequence"]
        bgs = extract_bigrams(seq)
        for a, b in bgs:
            recomputed_bigrams.add(f"{a}->{b}")

    batch_covered_bigrams = set(batch["coverage"]["bigrams"]["covered"])
    assert batch_covered_bigrams == recomputed_bigrams

    # Verify first selection is greedy optimal across the pool
    pool = generate_candidate_pool(master_seed=batch["seed"], pool_size=batch["pool"])
    max_bg_count = max(len(set(c.bigrams)) for c in pool)
    first_slug = tasks_info[0]["slug"]
    first_task_dir = batch_dir / first_slug
    first_gen = json.loads((first_task_dir / "generation.json").read_text(encoding="utf-8"))

    selected_cands, _ = select_candidates_by_coverage(pool, count=1)
    assert len(set(selected_cands[0].bigrams)) == max_bg_count
    assert selected_cands[0].sequence == first_gen["sequence"]


def test_h_provenance_integrity(sample_batch: tuple[Path, dict]) -> None:
    """provenance.json validates against ProvenanceMetadata and material_digest recomputes."""
    batch_dir, batch = sample_batch
    for task_info in batch["tasks"]:
        slug = task_info["slug"]
        task_dir = batch_dir / slug

        prov_path = task_dir / "provenance.json"
        assert prov_path.is_file()

        prov = ProvenanceMetadata.model_validate_json(prov_path.read_text(encoding="utf-8"))
        assert prov.item_id == slug
        assert prov.zone == "03-synthetic"
        assert prov.transform == "seqgen@0.1.0"
        assert len(prov.parent_digests) == 2

        # Recompute material_digest
        recomputed_digest = compute_package_manifest_digest(task_dir)
        assert prov.material_digest == recomputed_digest


def test_directory_immutability(tmp_path: Path) -> None:
    """generate_batch refuses to overwrite an existing non-empty directory."""
    out_dir = tmp_path / "non_empty"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dummy.txt").write_text("exists")

    with pytest.raises(FileExistsError, match="exists and is not empty"):
        generate_batch(
            seed=1,
            count=1,
            pool=10,
            out_dir=out_dir,
            now=datetime.now(UTC),
        )
