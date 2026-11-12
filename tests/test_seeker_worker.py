"""Stored witness replay and immutable custody, without an attacker or live queue."""

from __future__ import annotations

import json

from evallab.production.seeker_worker import (
    _parse_omp_json_stdout,
    _stream_served_route,
)


def test_omp_json_receipt_captures_final_text_and_served_model():
    message = {"role": "assistant", "provider": "test", "model": "synthetic", "content": [{"type": "text", "text": "stored"}]}
    raw = json.dumps({"type": "agent_end", "messages": [message]})
    assert _parse_omp_json_stdout(raw) == ("stored", "test", "synthetic")
    assert _stream_served_route([message, {**message, "model": "fallback"}], "test/synthetic") == ("test", "fallback")


