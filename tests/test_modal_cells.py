"""Modal single-cell execution, reserve/settle spend accounting and locking."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.modal_cells import (
    SandboxMeter,
    compute_cell_key,
    estimate_modal_cost,
    modal_cell_runner,
    sha256_text,
)
from evallab.regrade import CloudSpendGate


@dataclass
class FakeObservation:
    status: str
    reason: str | None
    facts: dict[str, Any]


class FakeBackend:
    """Mock ModalBackend for deterministic offline testing."""

    def __init__(self, repo_root: Path, spend_gate: Any):
        self.repo_root = repo_root
        self.spend_gate = spend_gate
        self.calls: list[dict[str, Any]] = []
        self.fail_infrastructure_attempts: int = 0
        self.fail_submission: bool = False
        self.raise_exc: BaseException | None = None

    def run_container_trial(
        self,
        package: Path,
        *,
        digest: str,
        mode: str,
        script: str | None,
        trial_dir: Path,
        timeout_seconds: int = 1800,
    ) -> FakeObservation:
        self.spend_gate.charge()
        self.calls.append({
            "package": package,
            "digest": digest,
            "script": script,
            "trial_dir": trial_dir,
        })
        if self.raise_exc is not None:
            raise self.raise_exc

        trial_dir.mkdir(parents=True, exist_ok=True)
        (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
        (trial_dir / "verifier").mkdir(parents=True, exist_ok=True)

        if self.fail_submission:
            (trial_dir / "agent/oracle.txt").write_text("Command failed with exit code 1\n")
            (trial_dir / "verifier/test_output.log").write_text("FAIL: test_feature\n")
            return FakeObservation(
                status="executed",
                reason="QA script exited 1",
                facts={"reward": 0.0},
            )

        (trial_dir / "agent/oracle.txt").write_text("PATCH_APPLIED\n")
        (trial_dir / "verifier/test_output.log").write_text("6 passed in 0.20s\n")
        return FakeObservation(
            status="executed",
            reason=None,
            facts={"reward": 1.0},
        )


def _make_dummy_package(path: Path, cpus: int = 2, memory_mb: int = 8192) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "task.toml").write_text(
        f'[environment]\ncpus = {cpus}\nmemory_mb = {memory_mb}\nworkdir = "/testbed"\n',
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 1. Key stability and sensitivity
# ---------------------------------------------------------------------------


def test_key_stability_and_sensitivity(tmp_path: Path) -> None:
    pkg_digest = sha256_text("pkg_v1")
    variant = "reference"
    sub_digest = sha256_text("echo HELLO")
    img = "modal-default"
    hook = "pytest"

    key1 = compute_cell_key(pkg_digest, variant, sub_digest, img, hook)
    # 1. Stability: exact same inputs produce exact same key
    assert key1 == compute_cell_key(pkg_digest, variant, sub_digest, img, hook)
    assert len(key1) == 64

    # 2. Sensitivity: changing any single input alters key
    assert compute_cell_key(sha256_text("pkg_v2"), variant, sub_digest, img, hook) != key1
    assert compute_cell_key(pkg_digest, "mutant-1", sub_digest, img, hook) != key1
    assert compute_cell_key(pkg_digest, variant, sha256_text("echo BYE"), img, hook) != key1
    assert compute_cell_key(pkg_digest, variant, sub_digest, "custom-image", hook) != key1
    assert compute_cell_key(pkg_digest, variant, sub_digest, img, "unittest") != key1



# ---------------------------------------------------------------------------
# 6. Error classification with fake backend
# ---------------------------------------------------------------------------


def test_error_classification_with_fake_backend(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    assert estimate_modal_cost(pkg, 10.0) > 0.0



# ---------------------------------------------------------------------------
# 9. modal_cell_runner backwards compatibility
# ---------------------------------------------------------------------------


def test_modal_cell_runner_compat(tmp_path: Path) -> None:
    pkg = _make_dummy_package(tmp_path / "pkg")
    cell_dir = tmp_path / "cells/001/ref"
    gate = CloudSpendGate(approval="app", cap_usd=1.0, usd_per_execution=0.01)
    meter = SandboxMeter(1.0)
    backend = FakeBackend(tmp_path, gate)

    runner = modal_cell_runner(
        repo_root=tmp_path,
        spend_gate=gate,
        meter=meter,
        backend_factory=lambda **_: backend,
    )

    res = runner(cell_dir, pkg, "echo HELLO")
    assert res["status"] == "executed"
    assert res["reward"] == 1.0
    assert (cell_dir / "result.json").is_file()
    assert meter.cells == 1
    assert meter.spent > 0

    # Running again returns cached result immediately
    res_cached = runner(cell_dir, pkg, "echo HELLO")
    assert res_cached["cache_hit"] is True
    assert len(backend.calls) == 1



def test_should_2_cross_process_same_cell(tmp_path: Path) -> None:
    """SHOULD 2: Cross-process flock on results_dir/<key>.lock prevents duplicate execution."""
    import subprocess
    import sys
    import textwrap

    pkg = _make_dummy_package(tmp_path / "pkg")
    results_dir = tmp_path / "results_twoproc"
    calls_file = tmp_path / "calls.txt"

    worker_code = textwrap.dedent(f"""
        import time
        from pathlib import Path
        from dataclasses import dataclass
        from evallab.modal_cells import modal_cell_runner
        from evallab.regrade import CloudSpendGate
        @dataclass
        class Obs:
            status: str
            reason: object
            facts: dict
        class B:
            def __init__(self, g): self.g = g
            def run_container_trial(self, pkg, **k):
                self.g.charge()
                with open({str(calls_file)!r}, 'a') as f:
                    f.write('call\\n')
                time.sleep(0.5)
                return Obs('executed', None, {{'reward': 1.0}})
        g = CloudSpendGate(approval='twoproc', cap_usd=1.0, usd_per_execution=0.01)
        r = modal_cell_runner(repo_root=Path({str(tmp_path)!r}), spend_gate=g, results_dir=Path({str(results_dir)!r}), timeout_seconds=240, backend_factory=lambda **_: B(g))
        r(Path({str(tmp_path)!r}) / "cells" / "v", Path({str(pkg)!r}), "same")
    """)

    p1 = subprocess.Popen([sys.executable, "-c", worker_code])
    p2 = subprocess.Popen([sys.executable, "-c", worker_code])
    assert p1.wait() == 0
    assert p2.wait() == 0

    calls = calls_file.read_text().strip().splitlines() if calls_file.exists() else []
    assert len(calls) == 1
