"""Behavioural tests for cyber ledger decisions (keep/exclude_train/discard)."""

from __future__ import annotations

import pytest

from evallab.cyber_ledger import (
    DupPair,
    OverlapEntry,
    RelatedEntry,
    SecbenchEntry,
    decide_verdicts,
    secbench_is_strong,
    split_group_for,
)


def _overlap(bug: str, gym: str = "arvo:1") -> OverlapEntry:
    return OverlapEntry(canonical_bug_id=bug, cybergym_ids=(gym,), match_type="exact")


def _secbench(sec_id: str, **kw) -> SecbenchEntry:
    args: dict[str, object] = {
        "secbench_id": sec_id,
        "split": "oss",
        "id_match": "",
        "same_fix": False,
        "crash_check": "function_and_type",
    }
    args.update(kw)
    return SecbenchEntry(**args)  # type: ignore[arg-type]


def test_secbench_strength_needs_link_plus_crash() -> None:
    assert secbench_is_strong([_secbench("a", same_fix=True)])
    assert secbench_is_strong([_secbench("a", id_match="plain")])
    assert not secbench_is_strong([_secbench("a", same_fix=True, crash_check="no_match")])
    assert not secbench_is_strong([_secbench("a")])
    assert not secbench_is_strong(
        [_secbench("a", same_fix=True, crash_check="names_fuzzer_entry_point")]
    )


def test_split_group_is_project_key() -> None:
    assert split_group_for("graphicsmagick") == "cyber:graphicsmagick"
def _fixture():
    """A task universe exercising every precedence rule.

    Interesting members: k1 plain keep; k2 related keep+flag; k3 weak-secbench
    keep+flag; o1 overlap; s1 suspect; s2o suspect+overlap; d1a/d1b identical
    dup pair (keep d1a); d2a overlap with twin d2b (discard d2b); d3a suspect
    with twin d3b under a differing prompt (keep d3b); b1a/b1b both-overlap
    pair; sb1 strong same-fix secbench; all else bulk fillers in disjoint
    `t`-ranges.
    """
    named = ["k1", "k2", "k3", "o1", "s1", "s2o",
             "d1a", "d1b", "d2a", "d2b", "d3a", "d3b",
             "b1a", "b1b", "sb1"]
    tasks = [f"t{i:04d}" for i in range(1000)] + named
    projects = {t: "proj" for t in tasks}
    projects.update({"k1": "skia", "k2": "opensips", "k3": "mupdf",
                     "s1": "php-src", "d3a": "tesseract", "d3b": "tesseract",
                     "sb1": "mruby"})
    sha = {t: f"sha-{t}" for t in tasks}

    overlap = {f"t{i:04d}": _overlap(f"bug-{i}") for i in range(273)}
    overlap.update({
        "o1": _overlap("bug-o1"),
        "s2o": _overlap("bug-s2o"),
        "d2a": _overlap("bug-d2"),
        "b1a": _overlap("bug-b"),
        "b1b": _overlap("bug-b"),
    })
    assert len(overlap) == 278

    suspects = {f"t{i:04d}" for i in range(620, 643)} | {"s1", "s2o", "d3a"}
    assert len(suspects) == 26

    pairs = [
        DupPair(canonical_bug_id=f"pair-{i}",
                first=f"t{i:04d}", second=f"t{i + 160:04d}", identical=True)
        for i in range(300, 460)
    ]
    pairs += [
        DupPair(canonical_bug_id="pair-d1", first="d1a", second="d1b", identical=True),
        DupPair(canonical_bug_id="pair-d2", first="d2a", second="d2b", identical=True),
        DupPair(canonical_bug_id="pair-d3", first="d3a", second="d3b", identical=False),
        DupPair(canonical_bug_id="pair-b", first="b1a", second="b1b", identical=True),
    ]
    assert len(pairs) == 164
    filler_seconds = {f"t{i + 160:04d}" for i in range(300, 460)}
    for i in range(300, 460):
        sha[f"t{i + 160:04d}"] = sha[f"t{i:04d}"]
    sha["d1a"] = sha["d1b"] = "sha-identical-pair"
    sha["d2a"] = sha["d2b"] = "sha-pair-d2"
    sha["b1a"] = sha["b1b"] = "sha-pair-b"
    # d3a/d3b keep distinct shas (differing prompts).

    related = {
        f"t{i:04d}": [RelatedEntry(mimo_bug=f"m{i}", cybergym_task_id="arvo:9",
                                   same_fix_commit=False)]
        for i in range(700, 718)
    }
    related["k2"] = [RelatedEntry(mimo_bug="m-k2", cybergym_task_id="arvo:53199",
                                  same_fix_commit=False)]
    assert len(related) == 19

    secbench: dict[str, list[SecbenchEntry]] = {
        f"t{i:04d}": [_secbench(f"bench-{i}", crash_check="no_match", same_fix=True)]
        for i in range(800, 822)
    }
    secbench["k3"] = [_secbench("mupdf.ossfuzz-1", crash_check="no_match", same_fix=True)]
    secbench["sb1"] = [_secbench("mruby.ossfuzz-1", same_fix=True)]
    assert len(secbench) == 24

    # Clean list = every keep plus every corroborated-SEC-bench exclusion.
    strong = {t for t, rows in secbench.items() if secbench_is_strong(rows)}
    assert strong == {"sb1"}
    losers = filler_seconds | {"d1b", "d2b"}
    clean = (set(tasks) - set(overlap) - suspects - losers) | strong
    return {
        "task_ids": tasks,
        "projects": projects,
        "instruction_sha": sha,
        "overlap": overlap,
        "suspects": suspects,
        "dup_pairs": pairs,
        "related": related,
        "secbench": secbench,
        "clean": clean,
        "fix_binary": set(),
    }


def test_decide_verdicts_precedence_and_flags() -> None:
    rows = decide_verdicts(**_fixture())

    assert rows["k1"].verdict == "keep"
    assert "related" in rows["k2"].reason and rows["k2"].verdict == "keep"
    assert "no_match" in rows["k3"].reason and rows["k3"].verdict == "keep"
    assert rows["o1"].verdict == "exclude_train"
    assert rows["s1"].verdict == "discard" and "bogus" in rows["s1"].reason
    assert rows["s2o"].verdict == "discard" and "CyberGym" in rows["s2o"].reason
    assert rows["d1a"].verdict == "keep" and rows["d1b"].verdict == "discard"
    assert rows["d1b"].dup_twin == "d1a" and "kept d1a" in rows["d1b"].reason
    assert rows["d2b"].verdict == "discard" and "twin is exclude_train" in rows["d2b"].reason
    assert rows["d3b"].verdict == "keep" and "real crash site" in rows["d3b"].reason
    assert rows["b1a"].verdict == rows["b1b"].verdict == "exclude_train"
    assert rows["sb1"].verdict == "exclude_train" and "SEC-bench" in rows["sb1"].reason
    # Twins share a split group.
    assert rows["d1a"].split_group == rows["d1b"].split_group == "cyber:proj"
    assert rows["d3a"].split_group == rows["d3b"].split_group == "cyber:tesseract"


def test_closed_world_rejects_unknown_ids() -> None:
    fix = _fixture()
    fix["overlap"]["ghost"] = fix["overlap"].pop("o1")
    with pytest.raises(ValueError, match="missing locally"):
        decide_verdicts(**fix)


def test_counts_are_pinned() -> None:
    fix = _fixture()
    del fix["overlap"]["o1"]
    with pytest.raises(ValueError, match="overlap tasks 277"):
        decide_verdicts(**fix)


def test_identical_prompt_mismatch_fails_closed() -> None:
    fix = _fixture()
    fix["instruction_sha"]["d1b"] = "sha-something-else"
    with pytest.raises(ValueError, match="listed identical but prompts differ"):
        decide_verdicts(**fix)
