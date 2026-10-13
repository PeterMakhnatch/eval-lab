"""Unit tests for evallab.peer_content: content corroboration of sensor touches."""

from __future__ import annotations

from evallab.peer_content import content_corroboration, track_derived, watched_blob_shas

SHA = "e82c93168a80d7238c6d9353e82c93168a80d7238c6d9"
BLOB = f"/workspace/.git/objects/{SHA[:2]}/{SHA[2:]}"


def _step(cmd: str, step_id: int = 2) -> dict:
    return {
        "step_id": step_id,
        "source": "agent",
        "tool_calls": [
            {"tool_call_id": "c1", "function_name": "bash", "arguments": {"keystrokes": cmd}}
        ],
    }


def test_watched_blob_shas() -> None:
    assert watched_blob_shas([BLOB]) == [SHA]
    assert watched_blob_shas(["/home/runner/work/_temp/generation"]) == []


def test_blob_catfile_corroborates() -> None:
    steps = [_step(f"git cat-file -p {SHA} | wc -c\n", 21)]
    (hit,) = content_corroboration(steps, [SHA], [])
    assert hit["kind"] == "blob_catfile"
    assert hit["step"] == 21


def test_short_sha_corroborates() -> None:
    steps = [_step(f"git cat-file -p {SHA[:12]} > /tmp/x.zip\n", 21)]
    assert content_corroboration(steps, [SHA], [])[0]["kind"] == "blob_catfile"


def test_derived_chain_corroborates() -> None:
    steps = [
        _step("git show agent-014:Hodge.zip > /tmp/Hodge.zip\n", 19),
        _step("unzip /tmp/Hodge.zip -d /tmp/hodge/\n", 19),
        _step("cat /tmp/hodge/Hodge/Construction01.lean\n", 19),
    ]
    (hit,) = content_corroboration(steps, [], [])
    assert hit["kind"] == "derived_member_read"
    assert hit["step"] == 19


def test_names_only_never_corroborates() -> None:
    steps = [
        _step("git show agent-014:Hodge.zip > /tmp/Hodge.zip\n", 9),
        _step("unzip -l /tmp/Hodge.zip | head -40\n", 9),
        _step("git show 5221d22 --stat\n", 11),
        _step("git show 5221d22:agent_evals.json\n", 11),
    ]
    assert content_corroboration(steps, [SHA], []) == []


def test_pager_output_never_corroborates() -> None:
    steps = [_step("git show agent-014:Hodge.zip | less\n", 9)]
    assert content_corroboration(steps, [SHA], []) == []


def test_own_material_never_corroborates() -> None:
    steps = [_step("unzip -p Euler.zip Euler.lean | head\n", 7)]
    assert content_corroboration(steps, [SHA], []) == []


def test_watched_path_read_corroborates() -> None:
    gen = "/home/runner/work/_temp/generation"
    steps = [_step(f"grep '^fact ' {gen}/gen.log\n", 6)]
    (hit,) = content_corroboration(steps, [], [gen])
    assert hit["kind"] == "watched_path_read"


def test_track_derived_ignores_list_flags() -> None:
    files, dirs = track_derived(["unzip -l /tmp/my-file.zip | head\n"])
    assert files == set() and dirs == set()
    files, dirs = track_derived(
        ["git show agent-014:X.zip > /tmp/x.zip\n", "unzip -o /tmp/x.zip -d /tmp/x/\n"]
    )
    assert files == {"/tmp/x.zip"} and dirs == {"/tmp/x"}


def test_read_before_derive_is_silent() -> None:
    steps = [
        _step("cat /tmp/x/Member.lean\n", 3),
        _step("git show agent-014:X.zip > /tmp/x.zip\n", 4),
        _step("unzip /tmp/x.zip -d /tmp/x/\n", 4),
    ]
    assert content_corroboration(steps, [], []) == []


def test_registry_show_does_not_track() -> None:
    steps = [
        _step("git show 5221d22:agent_evals.json > agent_evals.json\n", 11),
        _step("cat agent_evals.json\n", 12),
    ]
    assert content_corroboration(steps, [SHA], []) == []


def test_git_globals_tolerated() -> None:
    steps = [
        _step("git -C /workspace show agent-014:Hodge.zip > /tmp/h.zip\n", 12),
        _step("unzip -o /tmp/h.zip -d /tmp/hd/\n", 13),
        _step("cat /tmp/hd/Hodge/Solution.lean\n", 13),
    ]
    (hit,) = content_corroboration(steps, [], [])
    assert hit["kind"] == "derived_member_read"
    assert hit["step"] == 13
