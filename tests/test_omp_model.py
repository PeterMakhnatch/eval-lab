"""Historical parser and sealed native transport boundaries."""

import json

import pytest

from evallab.readers import omp_model


def end_event():
    return {
        "type": "agent_end",
        "messages": [{
            "role": "assistant",
            "provider": "kimi-code",
            "model": "k3.1-pro-highspeed",
            "stopReason": "stop",
            "responseId": "response-fixture",
            "content": [
                {"type": "thinking", "thinking": "private reasoning is not the answer"},
                {"type": "text", "text": '{"score":7,'},
                {"type": "text", "text": '"reason":"fixture","quotes":[]}'},
            ],
            "usage": {"input": 10, "output": 5, "cacheRead": 20, "cacheWrite": 3, "reasoningTokens": 2},
        }],
    }


def stream(event=None):
    return "diagnostic\n[]\n" + json.dumps(end_event() if event is None else event) + "\n"


def test_final_text_excludes_reasoning_and_preserves_native_usage():
    reply = omp_model.parse_omp_stream(stream())
    assert json.loads(reply.text)["score"] == 7
    assert "private reasoning" not in reply.text
    assert reply.served_model == omp_model.KIMI_MODEL
    assert reply.response_id == "response-fixture"
    assert (reply.input_tokens, reply.output_tokens, reply.cache_read_tokens) == (10, 5, 20)
    assert (reply.cache_write_tokens, reply.reasoning_tokens, reply.total_tokens) == (3, 2, 38)


@pytest.mark.parametrize("event", [
    {"type": "model_changed"},
    {"type": "model_fallback"},
    {"type": "FallbackAttempt"},
    {"type": "session", "resolvedModelIsFallback": True},
])
def test_any_fallback_event_rejects_even_matching_final_model(event):
    with pytest.raises(omp_model.OmpRouteError, match="fallback or change"):
        omp_model.parse_omp_stream(json.dumps(event) + "\n" + stream())


@pytest.mark.parametrize("field,value", [
    ("provider", "paid-provider"),
    ("model", "different-model"),
    ("model", None),
])
def test_identity_must_be_explicit_and_exact(field, value):
    event = end_event()
    event["messages"][0][field] = value
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event))


@pytest.mark.parametrize("field,value", [
    ("stopReason", "error"),
    ("stopReason", "length"),
    ("stopReason", "aborted"),
    ("refusal", "policy"),
    ("errorMessage", "cyber_policy"),
    ("content", [{"type": "refusal", "refusal": "no"}]),
    ("content", [{"type": "toolCall", "name": "bash"}]),
    ("content", [{"type": "text", "text": "I cannot assist with that request."}]),
    ("content", [{"type": "text", "text": "I'm sorry, I can't help with that."}]),
    ("content", [{"type": "text", "text": "I’m sorry, but I can’t assist with that request."}]),
    ("content", [{"type": "text", "text": "Sorry, but I cannot provide that."}]),
    ("content", [{"type": "text", "text": ""}]),
    ("content", [{"type": "thinking", "thinking": "only reasoning"}]),
    ("content", [{"type": "text", "text": None}]),
])
def test_refusal_incomplete_and_nontext_responses_are_not_completions(field, value):
    event = end_event()
    event["messages"][0][field] = value
    with pytest.raises(omp_model.OmpRouteError):
        omp_model.parse_omp_stream(stream(event))


@pytest.mark.parametrize("stdout", ["", "not json\n", '{"type":"agent_end"}', stream() + stream()])
def test_truncated_or_ambiguous_stream_fails_closed(stdout):
    with pytest.raises(omp_model.OmpRouteError):
        omp_model.parse_omp_stream(stdout)


@pytest.mark.parametrize("messages", [[], [end_event()["messages"][0]] * 2])
def test_exactly_one_final_assistant_turn(messages):
    with pytest.raises(omp_model.OmpRouteError, match="exactly one"):
        omp_model.parse_omp_stream(stream({"type": "agent_end", "messages": messages}))


@pytest.mark.parametrize("usage", [None, {}, {"input": True, "output": 5}, {"input": 1, "output": -1}])
def test_usage_cannot_be_missing_or_invented(usage):
    event = end_event()
    event["messages"][0]["usage"] = usage
    with pytest.raises(omp_model.OmpRouteError, match="usage"):
        omp_model.parse_omp_stream(stream(event))


def test_environment_keeps_oauth_home_but_removes_inherited_credentials():
    env = {
        "HOME": "/fixture/home", "PATH": "/bin", "OMP_PROFILE": "fixture",
        "OPENAI_API_KEY": "not-a-real-secret", "API_KEY": "fixture",
        "ANTHROPIC_OAUTH_TOKEN": "fixture", "KIMI_AUTH_TOKEN": "fixture",
        "CURSOR_ACCESS_TOKEN": "fixture", "A_KEYWORD": "not a credential",
    }
    assert omp_model.subscription_env(env) == {
        "HOME": "/fixture/home", "PATH": "/bin", "OMP_PROFILE": "fixture",
        "A_KEYWORD": "not a credential",
    }
    assert "OPENAI_API_KEY" in env  # Never mutate the parent environment.



def test_generic_route_attestation_includes_provider_and_full_model_id():
    event = end_event()
    event["messages"][0].update(provider="xai-oauth", model="grok-4.3")
    reply = omp_model.parse_omp_stream(stream(event), "xai-oauth/grok-4.3")
    assert reply.served_model == "xai-oauth/grok-4.3"
    with pytest.raises(omp_model.OmpRouteError, match="served-model mismatch"):
        omp_model.parse_omp_stream(stream(event), "kimi-code/k3.1-pro-highspeed")
