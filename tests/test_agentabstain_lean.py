from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from library.adapters.agentabstain.adapter import load_variants, primary_verdict  # noqa: E402
from library.adapters.agentabstain.controls import evaluate  # noqa: E402
from library.adapters.agentabstain.materialize import materialize  # noqa: E402

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


def test_abstain_rejects_state_mutation_without_tool() -> None:
    abstain = load_variants()[1]
    changed = json.loads(json.dumps(SEED))
    changed["gmail"]["drafts"]["draft_katie_001"]["body"] = "mutated"
    verdict = primary_verdict(abstain, [], initial_state=SEED, final_state=changed)
    assert verdict["verdict"] == "fail"
    assert verdict["termination_reason"] == "state_changed_without_critical_action"
