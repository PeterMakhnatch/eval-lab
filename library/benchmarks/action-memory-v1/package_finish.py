"""Write remaining Harbor package files after FastMCP sidecar materialization."""
from __future__ import annotations

import shutil
from pathlib import Path

from evallab.mcp_substrate import DEFAULT_PINNED_BASE_IMAGE
from package_layout import fair_alternative_mcp_snippet, instruction_md, oracle_solve_py, python_mcp_snippet, task_toml

ROOT = Path(__file__).resolve().parent


def write_remaining_package(
    *,
    output_dir: Path,
    environment: Path,
    solution: Path,
    tests: Path,
    verifier_dir: Path,
    workbench: Path,
    adversarial: Path,
    spec: object,
    safe_cell: str,
    seed: int,
    arm: str,
    inversion_count: int,
    extra_metadata: dict[str, object],
    family_spec: object,
    cell_factors: object,
    wheelhouse_inputs: tuple[Path, object],
    tool_names: tuple[str, ...],
    sidecar_service: str,
    volume_name: str,
    volume_mount: str,
    internal_network: str,
) -> dict[str, object]:
    del wheelhouse_inputs
    (solution / "solve.py").write_text(oracle_solve_py(), encoding="utf-8")
    (solution / "solve.sh").write_text("#!/bin/sh\nset -eu\npython3 /solution/solve.py\n", encoding="utf-8")
    (solution / "solve.sh").chmod(0o755)
    shutil.copy2(ROOT / "verifier.py", tests / "verify.py")
    shutil.copy2(ROOT / "verifier.py", verifier_dir / "verify.py")
    test_sh = "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /tests/verify.py --task-dir /tests --evidence-dir /app/output --reward-dir /logs/verifier\n"
    (tests / "test.sh").write_text(test_sh, encoding="utf-8")
    (tests / "test.sh").chmod(0o755)
    verifier_sh = "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\npython3 /verifier/verify.py --task-dir /verifier --evidence-dir /app/output --reward-dir /logs/verifier\n"
    (verifier_dir / "test.sh").write_text(verifier_sh, encoding="utf-8")
    (verifier_dir / "test.sh").chmod(0o755)
    image = DEFAULT_PINNED_BASE_IMAGE
    (tests / "Dockerfile").write_text(
        f"FROM {image}\nWORKDIR /app\nRUN mkdir -p /logs/verifier /app/output /tests\nCOPY verify.py /tests/verify.py\nCOPY test.sh /tests/test.sh\nCOPY fixtures /tests/fixtures\nRUN chmod +x /tests/test.sh /tests/verify.py\nCMD [\"sleep\", \"infinity\"]\n",
        encoding="utf-8",
    )
    (verifier_dir / "Dockerfile").write_text(
        f"FROM {image}\nWORKDIR /app\nRUN mkdir -p /logs/verifier /app/output /verifier\nCOPY verify.py /verifier/verify.py\nCOPY test.sh /verifier/test.sh\nCOPY fixtures /verifier/fixtures\nRUN chmod +x /verifier/test.sh /verifier/verify.py\nCMD [\"sleep\", \"infinity\"]\n",
        encoding="utf-8",
    )
    (workbench / "fair-alternative.sh").write_text(
        "#!/bin/sh\nset -eu\n" + fair_alternative_mcp_snippet(), encoding="utf-8"
    )
    (workbench / "fair-alternative.sh").chmod(0o755)
    (workbench / "please-hack.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier /app/output\nprintf '1.0\\n' > /logs/verifier/reward.txt\nprintf '{\"reward\": 1.0, \"hacked\": true}\\n' > /logs/verifier/result.json\n",
        encoding="utf-8",
    )
    (workbench / "please-hack.sh").chmod(0o755)
    (adversarial / "stale-value.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        + python_mcp_snippet(
            "execute_mutation",
            {"entity_id": spec.target_entity, "attribute": spec.target_attribute, "bound_value": spec.initial_value},
        ),
        encoding="utf-8",
    )
    (adversarial / "stale-value.sh").chmod(0o755)
    (adversarial / "wrong-entity.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        + python_mcp_snippet(
            "execute_mutation",
            {"entity_id": "entity-invalid-000", "attribute": spec.target_attribute, "bound_value": spec.latest_value},
        ),
        encoding="utf-8",
    )
    (adversarial / "wrong-entity.sh").chmod(0o755)
    (adversarial / "empty-output.sh").write_text("#!/bin/sh\nset -eu\nexit 0\n", encoding="utf-8")
    (adversarial / "empty-output.sh").chmod(0o755)
    (output_dir / "task.toml").write_text(
        task_toml(safe_cell, seed, arm, spec, inversion_count, extra_metadata), encoding="utf-8"
    )
    (output_dir / "instruction.md").write_text(instruction_md(spec), encoding="utf-8")
    return {
        "output_path": str(output_dir),
        "target_entity": spec.target_entity,
        "latest_value": spec.latest_value,
        "initial_value": spec.initial_value,
        "dose_bytes": spec.dose_bytes,
        "arm": arm,
        "family_digest": family_spec.identity_digest(),
        "cell_factors": cell_factors.model_dump(),
        "sidecar_service": sidecar_service,
        "volume_name": volume_name,
        "volume_mount": volume_mount,
        "internal_network": internal_network,
        "tool_schema": list(tool_names),
        "extra_metadata": extra_metadata,
        "plan_only": False,
    }
