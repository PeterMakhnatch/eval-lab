"""Focused tests for the native Scout/METR reader (pure helpers + real fixture)."""

import asyncio
import json
from pathlib import Path

import pytest

try:  # The reader needs the inspect dependency group; CI's default env lacks it.
    from inspect_ai.model import GenerateConfig, ResponseSchema

    from evallab.readers.scout_metr import (
        IMPLEMENTATION,
        MODEL,
        READER,
        JsonObjectTransport,
        parse_value,
        summarize_usage,
        to_verdict,
    )
except ImportError:
    HAVE_INSPECT = False
else:
    HAVE_INSPECT = True

pytestmark = pytest.mark.skipif(not HAVE_INSPECT, reason="inspect dependency group")

FIXTURES = Path(__file__).parent / "fixtures" / "scout_metr"


def test_reader_identity_and_implementation():
    assert READER == "scout_metr"
    assert MODEL == "openai/glm-5.3-flash"
    assert IMPLEMENTATION == ("METR reward_hacking scanner (native prompts; Z.ai transport shim)")


def test_usage_collapses_base_plus_cached():
    usage = {
        "openai/glm-5.3-flash": {
            "input_tokens": 1000,
            "input_tokens_cache_read": 400,
            "input_tokens_cache_write": 100,
            "output_tokens": 50,
            "total_tokens": 1550,
        }
    }
    assert summarize_usage(usage) == {"input": 1500, "cached": 400, "output": 50}


def test_usage_accepts_json_string_values():
    usage = {"openai/glm-5.3-flash": json.dumps({"input_tokens": 10, "output_tokens": 2})}
    assert summarize_usage(usage) == {"input": 10, "cached": 0, "output": 2}


def test_usage_empty_when_no_model_calls():
    assert summarize_usage({}) == {"input": 0, "cached": 0, "output": 0}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7.5, 7.5),
        (0, 0.0),
        (10, 10.0),
        ("10", 10.0),
        ("0", 0.0),
        ("7", 7.0),
        ("N", None),
        ("abc", None),
        (None, None),
        (True, None),
        (11, None),
        (-1, None),
    ],
)
def test_parse_value_keeps_only_in_range_scores(value, expected):
    assert parse_value(value) == expected


def test_to_verdict_preserves_score_without_inventing_native_threshold():
    verdict = to_verdict(
        row_id="tw-0001",
        value=8,
        explanation="edits the scoring script",
        model_usage={"m": {"input_tokens": 1000, "output_tokens": 100}},
        raw="/private/tmp/har187/scans/x",
    )
    assert verdict["id"] == "tw-0001"
    assert verdict["reader"] == "scout_metr"
    assert verdict["flagged"] is None
    assert verdict["implementation"] == IMPLEMENTATION
    assert verdict["score"] == 8.0
    assert verdict["model"] == MODEL
    assert verdict["tokens"] == {"input": 1000, "cached": 0, "output": 100}
    assert verdict["cost_usd"] == pytest.approx((1000 * 0.15 + 100 * 0.50) / 1_000_000)


def test_to_verdict_abstains_without_score():
    verdict = to_verdict(
        row_id="tw-0002",
        value="N",
        explanation=None,
        model_usage={},
        raw="/private/tmp/har187/scans/x",
    )
    assert verdict["flagged"] is None
    assert verdict["score"] is None
    assert verdict["cost_usd"] == 0.0


def test_real_native_row_parses_to_verdict():
    fixture = json.loads((FIXTURES / "native_row.json").read_text())
    verdict = to_verdict(
        row_id=fixture["id"],
        value=fixture["value"],
        explanation=fixture["explanation"],
        model_usage=fixture["model_usage"],
        raw=fixture["raw"],
    )
    assert verdict["reader"] == "scout_metr"
    assert verdict["score"] == pytest.approx(float(fixture["value"]))
    assert verdict["flagged"] is None
    assert verdict["tokens"]["input"] > 0
    assert verdict["cost_usd"] > 0


class _StubModel:
    def __init__(self) -> None:
        self.seen_configs = []

    async def generate(self, input, *args, **kwargs):
        self.seen_configs.append(kwargs.get("config"))
        return "ok"


def _schema_config():
    return GenerateConfig(
        response_schema=ResponseSchema(
            name="RewardHacking",
            json_schema={
                "type": "object",
                "properties": {"score": {"type": "number"}},
            },
        )
    )


def test_shim_rewrites_schema_to_json_object():
    stub = _StubModel()
    shim = JsonObjectTransport(stub)
    result = asyncio.run(shim.generate("prompt", config=_schema_config()))
    assert result == "ok"
    sent = stub.seen_configs[0]
    assert sent.response_schema is None
    assert sent.extra_body["response_format"] == {"type": "json_object"}


def test_shim_leaves_plain_calls_untouched():
    stub = _StubModel()
    shim = JsonObjectTransport(stub)
    plain = GenerateConfig()
    asyncio.run(shim.generate("prompt", config=plain))
    sent = stub.seen_configs[0]
    assert sent.response_schema is None
    assert not (sent.extra_body or {})


class _StubOutput:
    def __init__(self, completion):
        self.completion = completion

    def model_copy(self, update):
        return _StubOutput(update.get("completion", self.completion))


def test_shim_aliases_answer_key_inside_valid_json():
    stub = _StubModel()
    stub_output = _StubOutput('{"answer": "0", "reason": "r", "quotes": []}')
    stub.generate = lambda *a, **k: asyncio.sleep(0, result=stub_output)
    shim = JsonObjectTransport(stub)
    out = asyncio.run(shim.generate("prompt", config=_schema_config()))
    data = json.loads(out.completion)
    assert data["score"] == "0"
    assert "answer" not in data


def test_shim_leaves_non_json_and_scored_json_untouched():
    assert JsonObjectTransport._alias_answer_key("**Score: 0**") is None
    assert JsonObjectTransport._alias_answer_key('{"score": 5}') is None
    assert JsonObjectTransport._alias_answer_key(None) is None
