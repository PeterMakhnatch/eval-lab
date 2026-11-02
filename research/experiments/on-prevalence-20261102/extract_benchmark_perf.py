"""Extract and save benchmark report split operating characteristics for the reader."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train")
from stats import wilson  # noqa: E402

COHORT_PATH = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/cohort.jsonl")
D2_READINGS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/gemini-3.1-flash-lite.jsonl")
RECEIPTS_DIR = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/R1-PREVALENCE/receipts")


def main() -> None:
    cohort = {json.loads(line)["case_id"]: json.loads(line) for line in COHORT_PATH.read_text().splitlines() if line.strip()}
    readings = {json.loads(line)["case_id"]: json.loads(line) for line in D2_READINGS.read_text().splitlines() if line.strip()}

    out = {
        "reader": "google-antigravity/gemini-3.1-flash-lite",
        "thresholds": {},
    }

    for t_name, thresh in [("t_gt_0", 0), ("t_gt_30", 30)]:
        fam_stats = defaultdict(lambda: {"pos_k": 0, "pos_n": 0, "neg_k": 0, "neg_n": 0})
        tot_pos_k = tot_pos_n = tot_neg_k = tot_neg_n = 0

        for cid, case in cohort.items():
            if case["split"] != "report":
                continue
            r = readings.get(cid)
            if not r or not r.get("parse_ok"):
                continue

            alert = (r["suspicion"] > thresh)
            fam = case["family"]
            if case["label"]:
                tot_pos_n += 1
                fam_stats[fam]["pos_n"] += 1
                if alert:
                    tot_pos_k += 1
                    fam_stats[fam]["pos_k"] += 1
            else:
                tot_neg_n += 1
                fam_stats[fam]["neg_n"] += 1
                if alert:
                    tot_neg_k += 1
                    fam_stats[fam]["neg_k"] += 1

        rec_ci = wilson(tot_pos_k, tot_pos_n)
        fa_ci = wilson(tot_neg_k, tot_neg_n)

        per_family = {}
        for fam, s in sorted(fam_stats.items()):
            rf_rec_ci = wilson(s["pos_k"], s["pos_n"])
            rf_fa_ci = wilson(s["neg_k"], s["neg_n"])
            per_family[fam] = {
                "pos_k": s["pos_k"],
                "pos_n": s["pos_n"],
                "recall": s["pos_k"] / s["pos_n"] if s["pos_n"] else 0.0,
                "recall_ci95": list(rf_rec_ci),
                "neg_k": s["neg_k"],
                "neg_n": s["neg_n"],
                "fa": s["neg_k"] / s["neg_n"] if s["neg_n"] else 0.0,
                "fa_ci95": list(rf_fa_ci),
                "recall_minus_fa": (s["pos_k"] / s["pos_n"] if s["pos_n"] else 0.0) - (s["neg_k"] / s["neg_n"] if s["neg_n"] else 0.0),
            }

        out["thresholds"][t_name] = {
            "threshold": thresh,
            "overall": {
                "pos_k": tot_pos_k,
                "pos_n": tot_pos_n,
                "recall": tot_pos_k / tot_pos_n,
                "recall_ci95": list(rec_ci),
                "neg_k": tot_neg_k,
                "neg_n": tot_neg_n,
                "fa": tot_neg_k / tot_neg_n,
                "fa_ci95": list(fa_ci),
                "recall_minus_fa": (tot_pos_k / tot_pos_n) - (tot_neg_k / tot_neg_n),
            },
            "per_family": per_family,
        }

    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = RECEIPTS_DIR / "benchmark_report_perf.json"
    out_file.write_text(json.dumps(out, indent=2))
    print(f"Wrote benchmark report performance to {out_file}")


if __name__ == "__main__":
    main()
