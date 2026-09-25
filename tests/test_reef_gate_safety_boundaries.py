"""Safety-boundary regressions for the HAR-72 Reef gate adapter.

Four boundaries keep the read-only Reef checkout, the Lab application and the
gate's owned outputs apart, and each has a plausible silent failure mode:

* the calibration driver must refuse a work dir inside the Reef checkout
  before creating anything -- including ``--analyze-only`` runs -- and must
  resolve symlinks first, so no mode or path spelling can smuggle state into
  the checkout;
* the Reef subprocess PATH must keep the *symlinked* venv's bin directory
  first, so native launchers resolve inside the venv the interpreter was
  named from rather than the interpreter's eventual home;
* the gate plugin must reject malformed evaluation metadata and
  decision-record persistence failures instead of guessing an outcome, even
  for a candidate whose scores alone would publish;
* real Reef exception semantics are preserved verbatim (the native class
  name, no fabricated score, interrupts never converted into decisions)
  while the Lab application itself never imports Reef or this adapter.

Reef-dependent tests import the real Reef package inside the ``gate`` fixture
at execution time -- the same pattern as ``tests/test_reef_gate_plugin.py`` --
so this module still collects in the Lab environment where Reef is absent and
skips instead of running against a stand-in Reef.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from evallab_reef_gate import calibrate

#: The plugin's metadata key, repeated here only so the parametrize cases below
#: can be built before Reef is importable; each test asserts it still matches.
GATE_METADATA_KEY = "evallab_reef_gate"

#: The Lab application package root, for the import-boundary subprocess probe.
EVALLAB_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(scope="module")
def gate() -> SimpleNamespace:
    """The gate plugin and Reef's actual evaluation types, imported per run."""
    pytest.importorskip("reef", reason="real Reef integration runs in the separate Reef interpreter")
    from evallab_reef_gate.plugin import METADATA_KEY, Factory
    from reef.core.errors import ReefError
    from reef.core.evaluation import (
        CandidateEvaluator,
        EvaluationResult,
        UpdateCandidate,
    )
    from reef.train.cordis_backend.backend import HarnessCandidate

    class ScoredBackend(CandidateEvaluator):
        def __init__(self, candidate_scores: tuple, current_scores: tuple, repeats: int = 5) -> None:
            self.candidate_scores = candidate_scores
            self.current_scores = current_scores
            self.repeats = repeats

        def evaluate(self, candidate: UpdateCandidate) -> EvaluationResult:
            return EvaluationResult(
                evaluator="test_pairs", evaluator_version="1",
                metrics={
                    "candidate_scores": self.candidate_scores,
                    "current_scores": self.current_scores,
                    "episode_repeats": self.repeats,
                },
            )

    class ReefErrorBackend(CandidateEvaluator):
        def evaluate(self, candidate: UpdateCandidate) -> EvaluationResult:
            raise ReefError("native reef-side evaluation failure")

    class InterruptedBackend(CandidateEvaluator):
        def evaluate(self, candidate: UpdateCandidate) -> EvaluationResult:
            raise KeyboardInterrupt

    def candidate(identifier: str, tasks: int = 1) -> HarnessCandidate:
        return HarnessCandidate(
            candidate_id=identifier,
            candidate_files={}, current_files={}, candidate_entries=(), current_entries=(),
            mutations=(), evaluation_tasks=tuple(f"task {index}" for index in range(tasks)),
        )

    return SimpleNamespace(
        METADATA_KEY=METADATA_KEY,
        Factory=Factory,
        EvaluationResult=EvaluationResult,
        ScoredBackend=ScoredBackend,
        ReefErrorBackend=ReefErrorBackend,
        InterruptedBackend=InterruptedBackend,
        candidate=candidate,
    )


# -- calibration driver: the Reef checkout is not an output location ------------------------------


def checkout_like_root(tmp_path: Path) -> Path:
    """A temp directory shaped like a Reef checkout (package dir plus tutorial config)."""
    reef_root = tmp_path / "reef-checkout"
    (reef_root / "reef").mkdir(parents=True)
    config = reef_root / calibrate.SOURCE_CONFIG_RELATIVE
    config.parent.mkdir(parents=True)
    config.write_text("reef: {}\n")
    return reef_root


def snapshot(root: Path) -> set[str]:
    return {entry.relative_to(root).as_posix() for entry in root.rglob("*")}


def test_driver_refuses_work_dir_inside_the_checkout_for_every_mode(tmp_path: Path) -> None:
    """Both a fresh campaign and --analyze-only refuse before touching the checkout."""
    reef_root = checkout_like_root(tmp_path)
    before = snapshot(reef_root)
    fresh = reef_root / "gate-work"
    with pytest.raises(SystemExit, match="Reef checkout"):
        calibrate.main(["--reef-root", str(reef_root), "--work-dir", str(fresh)])
    assert not fresh.exists()

    existing = reef_root / "analyze-work"
    existing.mkdir()
    (existing / "run-meta.json").write_text("{}\n")
    with pytest.raises(SystemExit, match="Reef checkout"):
        calibrate.main(["--reef-root", str(reef_root), "--analyze-only", "--work-dir", str(existing)])
    assert snapshot(reef_root) == before | {"analyze-work", "analyze-work/run-meta.json"}


def test_driver_resolves_symlinked_work_dirs_before_the_checkout_refusal(tmp_path: Path) -> None:
    """A work dir named outside but linked inside is refused without creating its target."""
    reef_root = checkout_like_root(tmp_path)
    smuggled = reef_root / "smuggled-work"
    link = tmp_path / "outside-link"
    link.symlink_to(smuggled)
    with pytest.raises(SystemExit, match="Reef checkout"):
        calibrate.main(["--reef-root", str(reef_root), "--work-dir", str(link)])
    assert not smuggled.exists()


# -- calibration driver: the subprocess PATH follows the named venv ------------------------------


def test_child_environment_keeps_the_symlinked_venv_bin_first_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH is pinned to the symlink's bin dir (the venv's launchers), not the interpreter's home."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.symlink_to(Path(sys.executable).resolve())
    real_bin = Path(sys.executable).resolve().parent
    assert venv_bin != real_bin  # the fixture really crosses interpreter homes

    env = calibrate.child_environment(
        reef_root=Path("/nonexistent-reef"),
        work=tmp_path / "work",
        gate_config_path=tmp_path / "work" / "gate-config.json",
        python=python,
    )

    assert env["PATH"] == f"{venv_bin}{os.pathsep}/usr/bin:/bin"


# -- Lab application boundary --------------------------------------------------------------------


def test_importing_evallab_never_imports_reef_or_the_gate_adapter() -> None:
    """The application root stays importable without Reef; the adapter lives in its own process."""
    probe = (
        "import sys, evallab\n"
        "leaked = [name for name in sys.modules"
        " if name == 'reef' or name.startswith(('reef.', 'evallab_reef_gate'))]\n"
        "print(','.join(leaked))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PYTHONPATH": str(EVALLAB_SRC),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""


# -- gate plugin boundaries (real Reef types; skipped where Reef is absent) ----------------------


def test_record_dir_inside_the_reef_checkout_is_refused_before_creation(
    gate: SimpleNamespace, tmp_path: Path
) -> None:
    factory = gate.Factory(record_dir=tmp_path / "decisions")
    probe = factory.reef_root / "gate-records-boundary-probe"
    with pytest.raises(ValueError, match="Reef checkout"):
        gate.Factory(record_dir=probe)
    assert not probe.exists()


def test_symlinked_record_dir_cannot_smuggle_records_into_the_reef_checkout(
    gate: SimpleNamespace, tmp_path: Path
) -> None:
    factory = gate.Factory(record_dir=tmp_path / "decisions")
    target = factory.reef_root / "smuggled-gate-records"
    link = tmp_path / "outside-decisions"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="Reef checkout"):
        gate.Factory(record_dir=link)
    assert not target.exists()


@pytest.mark.parametrize(
    ("metrics", "metadata"),
    [
        pytest.param(
            {"candidate_scores": (1.0,) * 5, "current_scores": (0.0,) * 5, "episode_repeats": 5},
            None,
            id="metadata-not-a-mapping",
        ),
        pytest.param(
            {"candidate_scores": (1.0,) * 5, "current_scores": (0.0,) * 5, "episode_repeats": 5},
            {GATE_METADATA_KEY: "not-a-mapping"},
            id="gate-metadata-not-a-mapping",
        ),
        pytest.param(
            {"candidate_scores": (1.0,) * 5, "current_scores": (0.0,) * 5},
            {},
            id="missing-episode-repeats",
        ),
    ],
)
def test_decide_rejects_malformed_evaluation_metadata(
    gate: SimpleNamespace, tmp_path: Path, metrics: dict, metadata: object
) -> None:
    assert GATE_METADATA_KEY == gate.METADATA_KEY  # the literal above must track the plugin
    plugin = gate.Factory(record_dir=tmp_path / "decisions").build(gate.ScoredBackend((), ()))
    evaluation = gate.EvaluationResult(
        evaluator="test_pairs", evaluator_version="1", metrics=metrics, metadata=metadata
    )

    decision = plugin.decide(gate.candidate("malformed-evaluation"), evaluation)

    assert decision.outcome == "reject"
    assert decision.metrics["selected"] is False
    assert decision.metrics["reason_code"] == "invalid_evaluation"
    assert decision.metrics["p_value"] is None
    record = json.loads(Path(decision.metrics["decision_record"]).read_text())
    assert record["error_type"] in {"ValueError", "KeyError"}
    assert record["pairs"] == []


def test_unwritable_decision_record_rejects_a_would_be_publish(
    gate: SimpleNamespace, tmp_path: Path
) -> None:
    """A significant candidate is still rejected when its record cannot be made durable."""
    plugin = gate.Factory(record_dir=tmp_path / "decisions").build(
        gate.ScoredBackend((1.0,) * 5, (0.0,) * 5)
    )
    proposal = gate.candidate("unrecordable-publish")
    blocked = tmp_path / "decisions" / (
        hashlib.sha256(proposal.candidate_id.encode()).hexdigest() + ".json"
    )
    blocked.mkdir()  # exclusive creation cannot succeed against a directory

    decision = plugin.decide(proposal, plugin.evaluate(proposal))

    assert decision.outcome == "reject"
    assert decision.metrics["selected"] is False
    assert decision.metrics["reason_code"] == "decision_record_error"
    assert decision.metrics["error_type"] == "FileExistsError"
    assert decision.metrics["p_value"] == 0.03125  # significance was computed, not honoured


def test_real_reef_exception_type_is_recorded_verbatim(
    gate: SimpleNamespace, tmp_path: Path
) -> None:
    plugin = gate.Factory(record_dir=tmp_path / "decisions").build(gate.ReefErrorBackend())
    proposal = gate.candidate("reef-native-error")
    measured = plugin.evaluate(proposal)

    assert measured.metadata[gate.METADATA_KEY]["evaluation_error_type"] == "ReefError"
    decision = plugin.decide(proposal, measured)
    assert decision.outcome == "reject"
    assert decision.metrics["reason_code"] == "evaluator_error"
    record = json.loads(Path(decision.metrics["decision_record"]).read_text())
    assert record["error_type"] == "ReefError"
    assert record["p_value"] is None
    assert record["pairs"] == []


def test_interrupts_are_not_converted_into_gate_decisions(
    gate: SimpleNamespace, tmp_path: Path
) -> None:
    plugin = gate.Factory(record_dir=tmp_path / "decisions").build(gate.InterruptedBackend())
    with pytest.raises(KeyboardInterrupt):
        plugin.evaluate(gate.candidate("interrupted"))
