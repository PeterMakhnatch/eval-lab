"""Launch hidden job files through the submitted launcher and report what preflight did."""

import json
import sys
from pathlib import Path

from dispatch import launcher

work = Path(sys.argv[1])


def job(name: str, **fields) -> Path:
    path = work / f"{name}.json"
    path.write_text(json.dumps(fields))
    return path


def attempt(path: Path) -> dict:
    try:
        spec = launcher.launch(path)
    except launcher.PreflightError as exc:
        return {"outcome": "refused", "error": str(exc)}
    except Exception as exc:  # a crash is not a refusal
        return {"outcome": "crashed", "error": f"{type(exc).__name__}: {exc}"}
    return {"outcome": "launched", "tamper_evidence": spec.tamper_evidence, "workers": len(spec.workers)}


log = work / "frontier-ok.evidence.jsonl"
results = {
    "frontier_omitted_tier_with_log": attempt(
        job("frontier-ok", model_class="frontier", workers=2, evidence_log=str(log))
    ),
    "frontier_omitted_tier_no_log": attempt(job("frontier-nolog", model_class="frontier", workers=2)),
    "frontier_explicit_low_no_log": attempt(
        job("frontier-low", model_class="frontier", tier="low", workers=2)
    ),
    "astra_omitted_tier_no_log": attempt(job("astra-nolog", model_class="astra", workers=1)),
    "small_low": attempt(job("small", model_class="small", tier="low", workers=2)),
}
results["frontier_log_entries"] = (
    len(log.read_text().splitlines()) if log.exists() else 0
)
print(json.dumps(results))
