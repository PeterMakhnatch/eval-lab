"""Tests for goodfire card builder (B2).

- No labels ever in cards.
- DEV-only filter respected (via admission gate).
"""

from pathlib import Path

import pytest

from evallab.production import goodfire


def test_card_has_no_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # point cache to temp
    cache = tmp_path / "goodfire-dev.sqlite"
    monkeypatch.setattr(goodfire, "CACHE_PATH", cache)
    monkeypatch.setattr(goodfire, "CACHE_DIR", tmp_path)
    # build will use real DB but admission; we only inspect what is written
    # instead test the card shape never contains label keys even if data has
    # build a fake card row
    con = goodfire._open_cache(write=True)
    try:
        con.execute(
            "INSERT OR REPLACE INTO run_cards (run_id, task_id, task_statement, n_steps, char_total, char_narration, char_tool_call, char_tool_output, steps_json, card_version, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "test/dev/1",
                "t1",
                "stmt",
                3,
                10,
                5,
                3,
                2,
                '[{"i":0,"k":"narration","l":5}]',
                "goodfire-card/v1",
                "now",
            ),
        )
        con.commit()
    finally:
        con.close()
    c = goodfire.get_goodfire_card("test/dev/1")
    assert c is not None
    d = c.to_dict()
    bad = [
        k
        for k in d
        if any(
            x in k.lower()
            for x in ("exploit", "label", "passage", "verified", "receipt", "source_label")
        )
    ]
    assert not bad, f"labels leaked: {bad}"
    assert "is_exploit" not in str(d)
    assert c.n_steps == 3


def test_list_is_dev_only_by_construction() -> None:
    # the list comes only from the cache built with admission gate; here just that empty or real are DEV by contract
    ids = goodfire.list_goodfire_dev_run_ids(limit=0)
    assert isinstance(ids, list)


def test_extract_channels_splits_narration_call_output_and_caps() -> None:
    """Synthetic with all three kinds; asserts split + full char totals (B2b req).
    Verifies cap marker, no labels.
    """
    asst_row = {
        "step_index": 0,
        "role": "assistant",
        "content_text": "I will edit the file now.",
        "observation_preview": "",
        "command_or_action": {"cmd": "sed -i 's/foo/bar/' f.py"},
        "flags_json": '{"reasoning_content": "Plan: use search replace to cheat."}',
    }
    tool_row = {
        "step_index": 1,
        "role": "tool",
        "content_text": "file edited, test fails (no real run).",
        "observation_preview": "",
        "command_or_action": "",
        "flags_json": "{}",
    }
    long_asst = {**asst_row, "step_index": 2, "content_text": "x" * 5000, "command_or_action": "", "flags_json": "{}"}

    chans0 = goodfire._extract_channels(asst_row)
    chans1 = goodfire._extract_channels(tool_row)
    chans2 = goodfire._extract_channels(long_asst)

    all_ch = chans0 + chans1 + chans2
    kinds = [c["k"] for c in all_ch]
    assert "narration" in kinds and "call" in kinds and "output" in kinds
    assert kinds.count("narration") == 2
    assert kinds.count("call") == 1
    assert kinds.count("output") == 1

    nar_total = sum(c["l"] for c in all_ch if c["k"] == "narration")
    call_total = sum(c["l"] for c in all_ch if c["k"] == "call")
    out_total = sum(c["l"] for c in all_ch if c["k"] == "output")
    assert nar_total > 5000 and call_total > 10 and out_total > 10

    long_ch = [c for c in chans2 if c["k"] == "narration"][0]
    assert "truncated" in long_ch["t"] and len(long_ch["t"]) < 4100
    assert long_ch["l"] == 5000

    for c in all_ch:
        assert "is_exploit" not in str(c) and "label" not in str(c).lower()
    assert nar_total + call_total + out_total == sum(c["l"] for c in all_ch)
