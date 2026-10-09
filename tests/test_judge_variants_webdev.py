"""Consumer-visible webdev gates, full brief evidence and revision binding."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.task_variants import VariantInvalid
from evallab.webdev_brief_explicit import build_changes as brief_changes
from evallab.webdev_brief_explicit import build_grade_py as full_brief
from evallab.webdev_structural_gate import build_grade_py as structural_gate
from evallab.webdev_structural_gate import extract_keywords
from evallab.webdev_temp0_pin import PINNED_MODEL_SHA, build_grade_json, build_grade_py

GRADE = '''(V / "screenshot.jpg").write_bytes(jpg)
content = [{"type": "text", "text": build_prompt().format(query=cfg["query"][:1500])}]
body = json.dumps({"model": os.environ["WEBDEV_JUDGE_MODEL"], "temperature": 1.0,
                   "messages": [{"role": "user", "content": content}]}).encode()
def read_reply(r):
    if True:
        if True:
            msg = json.loads(r.read())["choices"][0]["message"]
    return msg
'''


class Finished(Exception):
    def __init__(self, reward, reason):
        self.reward = reward
        self.reason = reason


def execute(src: str, tmp_path: Path, *, text="Nexa insurance", revision=PINNED_MODEL_SHA,
            query="Nexa", metrics=True, scroll=1440):
    cfg = json.loads(build_grade_json(json.dumps({"cwd": "/workspace", "query": query})))
    cfg["gate_keywords"] = ["Nexa"]
    layout = {"text": text, "scroll_w": scroll, "client_w": 1440}
    def done(reward, reason, **extra):
        raise Finished(reward, reason)
    namespace = {"cfg": cfg, "V": tmp_path, "jpg": b"jpeg",
                 "shot": {"output": "LAYOUT:" + json.dumps(layout) if metrics else ""},
                 "os": SimpleNamespace(environ={"WEBDEV_JUDGE_REVISION": revision}),
                 "json": json, "build_prompt": lambda: "Brief: {query}", "done": done}
    try:
        exec(compile(src, "<derived-grader>", "exec"), namespace)
    except Finished as exc:
        return exc, namespace
    return None, namespace


@pytest.mark.parametrize(("text", "scroll", "reason"), [
    ("", 1440, "blank"), ("unrelated generic page", 1440, "brief-keyword"),
    ("Nexa insurance", 1442, "overflow"),
])
def test_failed_gates_score_zero_before_judge(tmp_path, text, scroll, reason):
    result, namespace = execute(structural_gate(GRADE), tmp_path, text=text, scroll=scroll)
    assert result.reward == 0.0
    assert reason in result.reason
    assert "body" not in namespace
    report = json.loads((tmp_path / "structural_gates.json").read_text())
    assert report["passed"] is False


def test_good_rendered_dom_reaches_judge(tmp_path):
    result, namespace = execute(structural_gate(build_grade_py(GRADE)), tmp_path)
    assert result is None
    body = json.loads(namespace["body"])
    assert body["temperature"] == 0
    assert body["model"].endswith(":novita")
    assert json.loads((tmp_path / "structural_gates.json").read_text())["passed"] is True


def test_missing_browser_metrics_mask_not_pass(tmp_path):
    result, namespace = execute(structural_gate(GRADE), tmp_path, metrics=False)
    assert result.reward is None
    assert "measurements" in result.reason
    assert "body" not in namespace


def test_long_brief_reaches_judge_without_tail_loss(tmp_path):
    query = "Nexa insurance " * 200 + "TAIL: must support accessibility and keyboard navigation."
    result, namespace = execute(full_brief(build_grade_py(GRADE)), tmp_path, query=query)
    assert result is None
    body = json.loads(namespace["body"])
    assert body["messages"][0]["content"][0]["text"] == "Brief: " + query
    provenance = json.loads((tmp_path / "brief.json").read_text())
    assert provenance["truncated"] is False
    assert provenance["sent_chars"] == len(query)
    assert len(provenance["query_sha256"]) == 64


def test_unbound_deployment_masks_before_request(tmp_path):
    result, namespace = execute(build_grade_py(GRADE), tmp_path, revision="other-revision")
    assert result.reward is None
    assert "unbound" in result.reason
    assert "body" not in namespace


def test_response_revision_must_match_task_pin(tmp_path):
    result, namespace = execute(build_grade_py(GRADE), tmp_path)
    assert result is None
    read_reply = namespace["read_reply"]
    def response(revision):
        return SimpleNamespace(read=lambda: json.dumps({
            "model_revision": revision, "choices": [{"message": {"content": "judgment"}}],
        }))
    assert read_reply(response(PINNED_MODEL_SHA)) == {"content": "judgment"}
    with pytest.raises(Finished) as exc:
        read_reply(response("floating"))
    assert exc.value.reward is None


def test_transform_builders_are_idempotent():
    for builder in (build_grade_py, structural_gate, full_brief):
        once = builder(GRADE)
        assert builder(once) == once


def test_unknown_grader_shape_is_refused():
    for builder in (build_grade_py, structural_gate, full_brief):
        with pytest.raises(VariantInvalid):
            builder("print('unrecognized grader')")


def test_short_brief_derivation_is_refused(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "task.toml").write_text('[task]\nname = "mimo-v2.6-rl/test"\n')
    (tmp_path / "tests/grade.py").write_text(GRADE)
    (tmp_path / "tests/grade.json").write_text(json.dumps({"cwd": "/workspace", "query": "Nexa"}))
    with pytest.raises(VariantInvalid, match="fits"):
        brief_changes(tmp_path)


def test_keywords_prioritize_explicit_names():
    keywords = extract_keywords('Brief: create a page for “Nexa Proteção”. Stack: CSS. Contact WhatsApp.')
    assert keywords[0] == "Nexa Proteção"
    assert "CSS" not in keywords
    assert extract_keywords('Brief: create a page for “Nexa Proteção”. Stack: CSS. Contact WhatsApp.') == keywords
