"""Length policy of sft.render_and_mask on the real tokenizer and fixture ($0).

    HF_HUB_OFFLINE=1 uv run --project tools/modal-mimo-sft --locked --with pytest \\
      pytest tools/modal-mimo-sft/test_sft.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sft

FIXTURE = Path(__file__).parent / "fixtures/tiny-export"


@pytest.fixture(scope="module")
def tokenizer():
    return sft.get_tokenizer()


@pytest.mark.parametrize("index", [0, 3])
def test_over_length_row_is_refused_never_truncated(tokenizer, index: int) -> None:
    row = sft.verify_export(FIXTURE).conversations[index]
    full = sft.render_and_mask(
        tokenizer, row.messages, max_length=sft.DEFAULT_MAX_LENGTH, label="row", loss=row.loss
    )
    exact = sft.render_and_mask(
        tokenizer, row.messages, max_length=len(full.input_ids), label="row", loss=row.loss
    )
    assert exact.input_ids == full.input_ids and exact.mask == full.mask
    with pytest.raises(sft.SftError, match="refused, never truncated"):
        sft.render_and_mask(
            tokenizer,
            row.messages,
            max_length=len(full.input_ids) - 1,
            label="row",
            loss=row.loss,
        )
