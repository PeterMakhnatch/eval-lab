"""Published-label cohort scoring boundaries (score_page.py cohort mode).

Only the uncertain edges: tampered/incomplete freezes abort, rater
disagreements leave the denominator, and missing/unknown page predictions
are explicit abstentions rather than ``none`` or silent drops.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_HAR119 = Path(__file__).resolve().parent.parent / "research/explorations/trace-lab/har119"


def _load_score_page():
    spec = importlib.util.spec_from_file_location("har119_score_page", _HAR119 / "score_page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


score_page = _load_score_page()


def _label_row(kind: str) -> dict:
    return {
        "trial": "t",
        "stop_reason": "x",
        "first_failure": None,
        "blame": "model",
        "loop_kind": kind,
        "loop_span": None,
        "pass_copied": None,
    }


def _write_labels(root: Path, kinds: dict[str, tuple[str, str]]) -> Path:
    """kinds maps trial -> (rater_a_kind, rater_b_kind); returns the label root."""
    for rater in ("rater_a", "rater_b"):
        (root / rater).mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []
    for trial, (kind_a, kind_b) in kinds.items():
        for rater, kind in (("rater_a", kind_a), ("rater_b", kind_b)):
            path = root / rater / f"{trial}.json"
            row = dict(_label_row(kind), trial=trial)
            path.write_text(json.dumps(row), encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.append(f"{digest}  {rater}/{trial}.json")
    (root / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    (root / "FROZEN_AT").write_text("2026-10-01T00:00:00Z\n", encoding="utf-8")
    return root


def _manifest_sha(labels: Path) -> str:
    return hashlib.sha256((labels / "MANIFEST.sha256").read_bytes()).hexdigest()


def _write_report(home: Path, trial: str, kind, name: str | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / f"trial-{trial}.json"
    path.write_text(
        json.dumps(
            {
                "trial_name": trial if name is None else name,
                "decision": {"judgments": {"loop_kind": {"kind": kind}}},
            }
        ),
        encoding="utf-8",
    )
    return path


def _score(*, labels: Path, home: Path, out: Path, trials: int):
    return score_page.score_published_cohort(
        labels_root=labels,
        results_home=home,
        output=out,
        expected_files=2 * trials,
        expected_trials=trials,
        expected_manifest_sha256=_manifest_sha(labels),
    )


def test_tampered_or_incomplete_freeze_aborts(tmp_path: Path) -> None:
    labels = _write_labels(tmp_path / "labels", {"t1": ("none", "none")})
    home = tmp_path / "home"
    _write_report(home, "t1", "none")

    tampered = labels / "rater_a" / "t1.json"
    tampered.write_text(json.dumps(dict(_label_row("repetition"), trial="t1")), encoding="utf-8")
    with pytest.raises(SystemExit):
        _score(labels=labels, home=home, out=tmp_path / "o.json", trials=1)

    _write_labels(tmp_path / "labels", {"t1": ("none", "none"), "t2": ("none", "none")})
    with pytest.raises(SystemExit):
        score_page.verify_published_freeze(labels, expected_files=2)


def test_rater_disagreement_leaves_the_denominator(tmp_path: Path) -> None:
    labels = _write_labels(
        tmp_path / "labels",
        {"agreed": ("none", "none"), "split": ("none", "repetition")},
    )
    home = tmp_path / "home"
    _write_report(home, "agreed", "none")
    _write_report(home, "split", "none")
    payload = _score(labels=labels, home=home, out=tmp_path / "o.json", trials=2)
    assert payload["rater_agreement"] == {"agree": 1, "n": 2}
    assert payload["disagreements"] == 1
    assert payload["page_vs_agreed"] == {"agree": 1, "n": 1}
    split = next(row for row in payload["rows"] if row["trial"] == "split")
    assert split["page_matches"] is None


def test_missing_or_unknown_page_is_an_abstention_not_none(tmp_path: Path) -> None:
    labels = _write_labels(
        tmp_path / "labels",
        {"gone": ("none", "none"), "weird": ("none", "none")},
    )
    home = tmp_path / "home"
    _write_report(home, "weird", "something-else")
    payload = _score(labels=labels, home=home, out=tmp_path / "o.json", trials=2)
    assert payload["page_abstentions"] == 2
    assert payload["page_vs_agreed"] == {"agree": 0, "n": 2}
    by_trial = {row["trial"]: row for row in payload["rows"]}
    assert by_trial["gone"]["page"] is None
    assert by_trial["gone"]["abstention_reason"]
    assert by_trial["weird"]["page"] is None
    assert by_trial["weird"]["abstention_reason"]


def test_wrong_manifest_sha_rejects_before_writing(tmp_path: Path) -> None:
    labels = _write_labels(tmp_path / "labels", {"t1": ("none", "none")})
    home = tmp_path / "home"
    _write_report(home, "t1", "none")
    out = tmp_path / "o.json"
    with pytest.raises(SystemExit):
        score_page.score_published_cohort(
            labels_root=labels,
            results_home=home,
            output=out,
            expected_files=2,
            expected_trials=1,
            expected_manifest_sha256="0" * 64,
        )
    assert not out.exists()


def test_report_for_another_trial_aborts(tmp_path: Path) -> None:
    labels = _write_labels(tmp_path / "labels", {"t1": ("none", "none")})
    home = tmp_path / "home"
    _write_report(home, "t1", "none", name="someone-else")
    with pytest.raises(SystemExit):
        _score(labels=labels, home=home, out=tmp_path / "o.json", trials=1)


def test_duplicate_manifest_cannot_leave_a_scored_label_unverified(tmp_path: Path) -> None:
    labels = _write_labels(tmp_path / "labels", {"t1": ("none", "none")})
    manifest = labels / "MANIFEST.sha256"
    first = manifest.read_text().splitlines()[0]
    manifest.write_text(f"{first}\n{first}\n")
    out = tmp_path / "o.json"
    with pytest.raises(SystemExit):
        _score(labels=labels, home=tmp_path / "home", out=out, trials=1)
    assert not out.exists()


def test_symlink_member_cannot_escape_frozen_root(tmp_path: Path) -> None:
    labels = _write_labels(tmp_path / "labels", {"t1": ("none", "none")})
    member = labels / "rater_a" / "t1.json"
    outside = tmp_path / "outside.json"
    member.replace(outside)
    member.symlink_to(outside)
    out = tmp_path / "o.json"
    with pytest.raises(SystemExit):
        _score(labels=labels, home=tmp_path / "home", out=out, trials=1)
    assert not out.exists()


def test_named_cohorts_have_distinct_metadata() -> None:
    specs = score_page.PUBLISHED_COHORTS
    assert set(specs) == {"har128-har116", "har128-g2-a1", "har128-g2-r2"}
    assert score_page.PUBLISHED_COHORT_DEFAULT == "har128-har116"
    har116, g2, r2 = specs["har128-har116"], specs["har128-g2-a1"], specs["har128-g2-r2"]
    assert len({g["cohort"] for g in (har116, g2, r2)}) == 3
    assert len({g["expected_manifest_sha256"] for g in (har116, g2, r2)}) == 3
    for spec in (har116, g2, r2):
        assert len(spec["expected_manifest_sha256"]) == 64
    assert len({str(g["output"]) for g in (har116, g2, r2)}) == 3
    assert (har116["expected_files"], har116["expected_trials"]) == (80, 40)
    assert (g2["expected_files"], g2["expected_trials"]) == (40, 20)
    assert (r2["expected_files"], r2["expected_trials"]) == (38, 19)


def test_default_cohort_resolves_to_current_har116_behavior() -> None:
    spec = score_page.resolve_published_cohort(None)
    assert spec["name"] == "har128-har116"
    assert spec["cohort"] == score_page.HAR128_COHORT
    assert spec["expected_manifest_sha256"] == score_page.HAR128_MANIFEST_SHA256
    with pytest.raises(SystemExit):
        score_page.resolve_published_cohort("no-such-study")


def test_cohort_description_flows_to_receipt_not_hardcoded_study(tmp_path: Path) -> None:
    labels = _write_labels(
        tmp_path / "labels",
        {
            "hit": ("none", "none"),
            "gone": ("none", "none"),
            "split": ("none", "repetition"),
        },
    )
    home = tmp_path / "home"
    _write_report(home, "hit", "none")
    payload = score_page.score_published_cohort(
        labels_root=labels,
        results_home=home,
        output=tmp_path / "o.json",
        expected_files=6,
        expected_trials=3,
        expected_manifest_sha256=_manifest_sha(labels),
        cohort="STUDY UNDER TEST",
        heldout="heldout under test",
        limit="limit under test",
    )
    assert payload["cohort"] == "STUDY UNDER TEST"
    assert payload["heldout"] == "heldout under test"
    assert payload["limit"] == "limit under test"
    assert payload["rater_agreement"] == {"agree": 2, "n": 3}
    assert payload["disagreements"] == 1
    assert payload["page_vs_agreed"] == {"agree": 1, "n": 2}
    assert payload["page_abstentions"] == 1
