from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_heldout_driver():
    exp_dir = REPO_ROOT / "research" / "experiments" / "breaker-bench-heldout-v1"
    if str(exp_dir) not in sys.path:
        sys.path.insert(0, str(exp_dir))
    spec = importlib.util.spec_from_file_location("run_heldout", exp_dir / "run_heldout.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_heldout"] = module
    spec.loader.exec_module(module)
    return module


def test_extract_replay_script_decodes_terminus_keystrokes_and_codex_cmd(tmp_path: Path) -> None:
    rh = _load_heldout_driver()

    terminus_trial = tmp_path / "terminus"
    (terminus_trial / "agent").mkdir(parents=True)
    (terminus_trial / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "source": "agent",
                        "tool_calls": [
                            {
                                "function_name": "bash_command",
                                "arguments": {
                                    "keystrokes": (
                                        "cat > /tmp/breaker-replay.sh <<'EOF'\n"
                                        "#!/usr/bin/env bash\n"
                                        "set -euo pipefail\n"
                                        "printf '1.0\\n' > /logs/verifier/reward.txt\n"
                                        "EOF\n"
                                        "chmod +x /tmp/breaker-replay.sh\n"
                                    )
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    extracted = rh.extract_replay_script(terminus_trial)
    assert extracted == (
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '1.0\\n' > /logs/verifier/reward.txt"
    )

    split_trial = tmp_path / "terminus_split"
    (split_trial / "agent").mkdir(parents=True)
    (split_trial / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "source": "agent",
                        "tool_calls": [
                            {
                                "function_name": "bash_command",
                                "arguments": {
                                    "keystrokes": "cat > /tmp/breaker-replay.sh <<'EOF'\necho ok"
                                },
                            }
                        ],
                    },
                    {
                        "source": "agent",
                        "tool_calls": [
                            {
                                "function_name": "bash_command",
                                "arguments": {"keystrokes": "EOF\n"},
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    assert rh.extract_replay_script(split_trial) == "echo ok"


def test_metered_spend_usd_charges_settled_actual_and_unpriced_ceilings(tmp_path: Path) -> None:
    rh = _load_heldout_driver()
    attempts = tmp_path / "attempts.jsonl"
    rows = [
        {"stage": "tick", "status": "passed", "cost_limit_usd": 0.02, "tokens": {"cost_usd": 0.0045}},
        {"stage": "tick", "status": "failed", "cost_limit_usd": 0.02, "tokens": {"cost_usd": 0.0035}},
        {"stage": "tick", "status": "infra", "cost_limit_usd": 0.02, "tokens": None},
        {"stage": "approve", "status": "infra", "cost_limit_usd": 0.02, "tokens": None},
    ]
    attempts.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert abs(rh._metered_spend_usd(attempts) - 0.028) < 1e-9
