"""POST-HOC, not preregistered, run once after the preregistered results were written. Descriptive.

(1) All four candidates and the current gate on the 2,142 strict-DEV runs outside the 246 pair runs: alarm rate by class
    within length strata, and a Mantel-Haenszel common odds ratio (enacted vs clean) over the strata, with the
    Robins-Breslow-Greenland 95% interval. Strata: the NP31 tertiles (cut points 40 and 89) and ten length-quantile bins.
(2) Whether the test-run recognizer of C2/C4 sees any test run in the NP33 staged traces (it is the reason C2/C4 alarm on none
    of them).
"""

from __future__ import annotations

import json
import math
import pickle
from pathlib import Path

import data
import gate
import score

RESULTS = Path(__file__).resolve().parent.parent / "results"


def mantel_haenszel(strata: list[tuple[int, int, int, int]]) -> dict:
    """Strata ``(a, b, c, d)`` = (enacted alarmed, enacted not, clean alarmed, clean not)."""
    r = s = pr = ps_qr = qs = 0.0
    for a, b, c, d in strata:
        n = a + b + c + d
        if n == 0:
            continue
        r_i, s_i = a * d / n, b * c / n
        p_i, q_i = (a + d) / n, (b + c) / n
        r += r_i
        s += s_i
        pr += p_i * r_i
        ps_qr += p_i * s_i + q_i * r_i
        qs += q_i * s_i
    if r == 0 or s == 0:
        return {"odds_ratio": None}
    odds = r / s
    var = pr / (2 * r * r) + ps_qr / (2 * r * s) + qs / (2 * s * s)
    half = 1.959964 * math.sqrt(var)
    return {
        "odds_ratio": round(odds, 2),
        "ci95": [
            round(math.exp(math.log(odds) - half), 2),
            round(math.exp(math.log(odds) + half), 2),
        ],
    }


def main() -> int:
    rows = score.alarms("goodfire_dev")
    cls = score.labelled("goodfire_dev", "goodfire-dev")
    pair_runs = {r for p in data.pair_rows() for r in (p["enacted"], p["clean"])}
    ids = [r for r in rows if r in cls and r not in pair_runs]
    lengths = sorted(rows[r]["length"] for r in ids)
    decile_cuts = [
        lengths[min(len(lengths) - 1, math.ceil(k * len(lengths) / 10) - 1)] for k in range(1, 10)
    ]

    def stratum_tertile(length: int) -> int:
        return score.tertile(length)

    def stratum_decile(length: int) -> int:
        return sum(length > cut for cut in decile_cuts)

    out: dict = {
        "runs": len(ids),
        "enacted": sum(cls[r] for r in ids),
        "clean": sum(not cls[r] for r in ids),
        "rules": {},
    }
    for name in score.NAMES:
        block: dict = {}
        for label, stratum in (("tertiles", stratum_tertile), ("deciles", stratum_decile)):
            cells: dict[int, list[int]] = {}
            for r in ids:
                a_b_c_d = cells.setdefault(stratum(rows[r]["length"]), [0, 0, 0, 0])
                fired = bool(rows[r][name])
                a_b_c_d[(0 if fired else 1) if cls[r] else (2 if fired else 3)] += 1
            block[label] = {
                "mantel_haenszel": mantel_haenszel([tuple(v) for v in cells.values()]),
                "by_stratum": {
                    str(k): {
                        "enacted": score.rate(v[0], v[0] + v[1]),
                        "clean": score.rate(v[2], v[2] + v[3]),
                    }
                    for k, v in sorted(cells.items())
                },
            }
        out["rules"][name] = block
    runs33 = pickle.loads((score.CACHE / "np33.pkl").read_bytes())
    seen = sum(bool(gate.test_runs(gate.agent_steps_of(steps))) for steps in runs33.values())
    out["np33_runs_where_the_test_run_recognizer_sees_a_test_run"] = {"k": seen, "n": len(runs33)}
    (RESULTS / "posthoc.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    for name, block in out["rules"].items():
        print(
            name,
            "MH tertiles",
            block["tertiles"]["mantel_haenszel"],
            "deciles",
            block["deciles"]["mantel_haenszel"],
        )
    print(out["np33_runs_where_the_test_run_recognizer_sees_a_test_run"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
