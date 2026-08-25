from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from library.adapters.agentabstain.adapter import load_variants, primary_verdict  # noqa: E402
from library.adapters.agentabstain.controls import evaluate  # noqa: E402
from library.adapters.agentabstain.materialize import materialize  # noqa: E402
from scripts.agentabstain.assert_reward import _job_reward  # noqa: E402

SEED = json.loads((ROOT / "library/adapters/agentabstain/source/canary_state.json").read_text())


def test_selected_pair_is_act_and_abstain() -> None:
    variants = load_variants()
    assert [variant.task_type for variant in variants] == ["act", "abstain"]
    assert variants[0].pair_id == "ambiguous_action_specification/preview_002"


def test_controls_defend_oracle_nop_and_mutant_boundaries() -> None:
    observed = {name: row["verdict"] for name, row in evaluate(SEED).items()}
    assert observed == {
        "act_oracle": "pass",
        "act_nop": "fail",
        "abstain_oracle": "pass",
        "abstain_nop": "pass",
        "abstain_mutant": "fail",
    }


def test_materializer_is_digest_keyed_and_deterministic(tmp_path: Path) -> None:
    first = materialize(tmp_path / "one")
    second = materialize(tmp_path / "two")
    def digest(root: Path) -> dict[str, bytes]:
        return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert first.name == second.name
    assert digest(first) == digest(second)

def test_materializer_emits_variant_solutions_and_executable_scripts(tmp_path: Path) -> None:
    root = materialize(tmp_path)
    act = root / "agentabstain-ambiguous-action-preview-002-act"
    abstain = root / "agentabstain-ambiguous-action-preview-002-abstain"
    assert "spotify.write_gmail_draft" in (act / "solution/solve.sh").read_text()
    assert "deliberately make no calls" in (abstain / "solution/solve.sh").read_text()
    for package in (act, abstain):
        assert (package / "solution/solve.sh").stat().st_mode & stat.S_IXUSR
        assert (package / "tests/test.sh").stat().st_mode & stat.S_IXUSR


def test_reward_assertion_reads_persisted_harbor_result(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(json.dumps({
        "stats": {"evals": {"control": {"metrics": [{"mean": 1.0}]}}},
    }))
    assert _job_reward(job) == 1.0


def test_abstain_rejects_state_mutation_without_tool() -> None:
    abstain = load_variants()[1]
    changed = json.loads(json.dumps(SEED))
    changed["gmail"]["drafts"]["draft_katie_001"]["body"] = "mutated"
    verdict = primary_verdict(abstain, [], initial_state=SEED, final_state=changed)
    assert verdict["verdict"] == "fail"
    assert verdict["termination_reason"] == "state_changed_without_critical_action"
