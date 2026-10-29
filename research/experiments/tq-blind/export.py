#!/usr/bin/env python3
"""Write the committed TQ-BLIND ``results.json`` from the private scored state (no model call, no label text).

Adds what the preregistered ``score`` stage does not compute: per-arm verdict totals, flip-rate comparisons of the
redacted arm against the unredacted re-run (two-sided Fisher exact), and opaque verdict rows. Rows carry only unit
and finding ids, the unit kind, the known-real indicator, gate attribution and verdicts.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from math import comb
from pathlib import Path
from typing import Any

STATE = Path.home() / ".local/state/evallab-fleet/2026-10-29/TQ-BLIND"


def fisher_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for the table [[a, b], [c, d]] (sum of tables no more likely than observed)."""
    row1, col1, total = a + b, a + c, a + b + c + d

    def prob(x: int) -> float:
        return comb(col1, x) * comb(total - col1, row1 - x) / comb(total, row1)

    observed = prob(a)
    low, high = max(0, row1 - (total - col1)), min(row1, col1)
    return min(1.0, sum(p for x in range(low, high + 1) if (p := prob(x)) <= observed * (1 + 1e-9)))


def main() -> None:
    private: dict[str, Any] = json.loads((STATE / "results-private.json").read_text())
    rows = private.pop("rows")
    out = {k: v for k, v in private.items()}
    totals = {
        arm: {
            who: dict(Counter(r[who] or "missing" for r in arm_rows))
            for who in ("sol", "opus", "final")
        }
        for arm, arm_rows in rows.items()
    }
    out["verdict_totals"] = totals
    flips = out["flips"]
    out["flip_rate_comparison"] = {
        who: {
            "redacted_flips": flips["redacted"][who]["flips"],
            "control_flips": flips["control"][who]["flips"],
            "n": flips["redacted"][who]["n"],
            "fisher_two_sided_p": round(
                fisher_two_sided(
                    flips["redacted"][who]["flips"],
                    flips["redacted"][who]["n"] - flips["redacted"][who]["flips"],
                    flips["control"][who]["flips"],
                    flips["control"][who]["n"] - flips["control"][who]["flips"],
                ),
                4,
            ),
        }
        for who in ("sol", "opus", "final")
        if "control" in flips
    }
    out["verdict_rows"] = [
        {
            "unit_id": o["unit_id"],
            "finding_id": o["finding_id"],
            "kind": o["kind"],
            "gates": o["gates"],
            "known_real": o["known_real"],
            **{f"{who}_{arm}": rows[arm][i][who] for arm in rows for who in ("sol", "opus")},
        }
        for i, o in enumerate(rows["original"])
    ]
    target = Path(sys.argv[1])
    target.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(json.dumps(out["flip_rate_comparison"], indent=1), json.dumps(totals, indent=1), sep="\n")


if __name__ == "__main__":
    main()
