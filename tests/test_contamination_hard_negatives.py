"""The 38 Kev false alarms are published as documented hard negatives.

`kev_hard_negatives.jsonl` must stay in agreement with the audit labels and the
detectors-v1 source: one row per clean pass the Kev-4B union flagged, each with
both judges' clearance and margins over the Terminal Wrench cuts. Uses only
committed files (labels, manifest, results), never the gitignored score dumps.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "research/experiments/contamination-audit"
CAL = REPO / "research/calibration/contamination-audit"


def _jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _labels() -> dict[str, dict]:
    return {row["trial"]: row for row in _jsonl(CAL / "labels.jsonl")}


def _manifest_ca() -> list[dict]:
    return [
        json.loads(line)
        for line in (REPO / "research/calibration/detectors-v1/manifest.jsonl").read_text().splitlines()
        if json.loads(line)["source"] == "contamination-audit"
    ]


def test_hard_negatives_match_labels_and_source() -> None:
    hard = _jsonl(AUDIT / "results/kev_hard_negatives.jsonl")
    labels = _labels()
    flagged = sorted(
        row["id"][len("ca-") :] for row in _manifest_ca() if row.get("kev_flagged") and not row["labels"]["reward_hacking"]
    )
    assert len(hard) == len(flagged) == 38
    assert sorted(row["trial"] for row in hard) == flagged
    for row in hard:
        label = labels[row["trial"]]
        assert label["verdict"] == "clean"
        assert row["detectors_row"] == "ca-" + row["trial"]
        assert label["judges"]["sol"]["cheated"] is False
        assert label["judges"]["claude"]["cheated"] is False
        assert row["firing_cards"], row["trial"]
        for card, hit in row["firing_cards"].items():
            assert hit["margin"] == round(hit["score"] - hit["cut_5pct_tw"], 4), (row["trial"], card)
            assert hit["score"] > hit["cut_5pct_tw"], (row["trial"], card)


def test_cut_analysis_agrees_with_hard_negatives() -> None:
    analysis = json.loads((AUDIT / "results/kev_cut_analysis.json").read_text())
    hard = _jsonl(AUDIT / "results/kev_hard_negatives.jsonl")
    base = analysis["candidates"]["union_7_status_quo"]["cells"]["contamination-audit"]
    assert (base["caught"], base["denominator"], base["clean_flagged"]) == (30, 30, 38)
    rec = analysis["candidates"]["recommended_ca_cards_global"]["cells"]["contamination-audit"]
    assert (rec["caught"], rec["denominator"]) == (30, 30)
    assert rec["clean_flagged"] == sum(1 for row in hard if row["kept_by_recommended_ca_cards"])
    assert analysis["contamination_audit"]["kev_false_alarms"] == len(hard) == 38
    for card, group in analysis["per_card"].items():
        assert group["cut_5pct_tw"] > 0, card
        assert group["cheat_margin"]["n"] + group["clean_margin"]["n"] > 0, card
