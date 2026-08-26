#!/usr/bin/env python3
"""Linux lane preflight: materialization, reproducibility, controls, corpus guard."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _tree_digest(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def main() -> None:
    from library.adapters.agentabstain.adapter import load_variants
    from library.adapters.agentabstain.controls import evaluate
    from library.adapters.agentabstain.materialize import (
        assert_no_committed_generated,
        materialize,
        source_id,
    )
    assert_no_committed_generated(ROOT)
    variants = load_variants()
    if len(variants) != 2 or {item.task_type for item in variants} != {"act", "abstain"}:
        raise RuntimeError("selected canary pair missing")
    generated = materialize()
    first = _tree_digest(generated)
    with tempfile.TemporaryDirectory() as temporary:
        second_root = Path(temporary) / "harbor-tasks"
        second = materialize(second_root)
        if first != _tree_digest(second):
            raise RuntimeError("materializer is not deterministic")
    seed = json.loads((ROOT / "library/adapters/agentabstain/source/canary_state.json").read_text())
    outcomes = evaluate(seed)
    expected = {
        "act_oracle": "pass", "act_nop": "fail", "act_mutant": "fail",
        "abstain_oracle": "pass", "abstain_nop": "pass", "abstain_mutant": "fail",
    }
    observed = {name: row["verdict"] for name, row in outcomes.items()}
    if observed != expected:
        raise RuntimeError(f"control matrix mismatch: {observed}")
    print(json.dumps({"source_digest": source_id(), "generated": str(generated), "controls": observed}, sort_keys=True))


if __name__ == "__main__":
    main()
