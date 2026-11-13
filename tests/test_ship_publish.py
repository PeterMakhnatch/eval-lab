"""Publication and unchanged-patch guards with the shared real-Git fixture."""
from __future__ import annotations

import argparse
import json

from test_ship import delivery as delivery

from evallab.ship import run_command


def publish_args(*arguments):
    from evallab.ship import register_parser

    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers())
    return parser.parse_args(["ship", *arguments])


def publication_files(state):
    title = state.parent / "title.txt"
    body = state.parent / "body.md"
    title.write_text("fix: preserve complete delivery evidence\n")
    body.write_text("## Change\nPreserve the original PR text and add later evidence as comments.\n\n## Proof\nThe focused delivery cases pass.\n")
    return title, body


def test_publication_opens_once_and_comments_without_edit(delivery, capsys, monkeypatch):
    _, root, tree, state, _, change = delivery
    monkeypatch.setenv("HOME", str(state.parent / "home"))
    title, body = publication_files(state)
    args = publish_args("open", "--title-file", str(title), "--body-file", str(body))
    change(open_prs=[])
    for content, message in [
        (" \n", "Publication refused: empty body; write the complete text in --body-file."),
        ("## Summary\n## Proof\n", "Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details."),
        ("## Summary\n- [ ] TODO\n<!-- complete later -->", "Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details."),
        ("A real summary.\n\n## Proof\nTBD", "Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details."),
    ]:
        body.write_text(content)
        assert run_command(args, tree) == 1
        assert capsys.readouterr().out.strip() == message
    body.write_text("Remove obsolete TODO checks.\n\nProof: the delivery cases pass.\n")
    assert run_command(args, tree) == 0
    original = json.loads(state.read_text())["created"]
    capsys.readouterr()
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: PR 42 already exists; add evidence with evallab ship comment 42 "
        "--body-file <file>. Do not edit its title or body."
    )
    remote = json.loads(state.read_text())["pr"]
    remote["state"] = "MERGED"
    change(pr=remote)
    body.write_text("Merged revision CLI smoke passes.\n")
    assert run_command(publish_args("comment", "42", "--body-file", str(body)), root) == 0
    result = json.loads(state.read_text())
    assert result["created"] == original
    assert result["comments"] == [{"pr": "42", "body": "Merged revision CLI smoke passes.\n"}]
    assert not any(call[:2] == ["pr", "edit"] for call in result["calls"])
    assert not args.state_dir.exists()


def test_publication_refuses_unpushed_head(delivery, capsys):
    _, _, tree, state, git, change = delivery
    title, body = publication_files(state)
    args = publish_args("open", "--title-file", str(title), "--body-file", str(body))
    change(open_prs=[])
    (tree / "consumer.txt").write_text("needed commit\n")
    git("commit", "-am", "needed fix", cwd=tree)
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: topic HEAD is not pushed; run make prepush, then git push -u origin topic."
    )
    assert not json.loads(state.read_text()).get("created")
    git("push", "origin", "topic", cwd=tree)
    assert run_command(args, tree) == 0


def test_check_push_refuses_unchanged_patch_and_accepts_needed_change(delivery, capsys):
    _, root, tree, state, git, _ = delivery
    args = publish_args("check-push", "42")
    remote = json.loads(state.read_text())["pr"]["headRefOid"]
    message = (f"Push refused: same patch as PR head {remote}; main advancing does not require a new head "
               "(ship accepts it). Push only for a real conflict or a needed commit: add --needs-base <sha> with a reason.")
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == message
    (root / "unrelated.txt").write_text("main advances without overlap\n")
    git("add", "unrelated.txt")
    git("commit", "-m", "main advancement")
    git("push", "origin", "main")
    base = git("rev-parse", "HEAD")
    git("rebase", "main", cwd=tree)
    assert git("rev-parse", "HEAD", cwd=tree) != remote
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == message
    args.needs_base = "not-a-sha"
    args.reason = "The change needs the new base API."
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Push refused: --needs-base must name a commit SHA reachable from HEAD and origin/main."
    )
    args.needs_base = base
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == f"Push check passed: explicit base dependency {base}; reason: {args.reason}"
    (tree / "consumer.txt").write_text("needed behavior fix\n")
    git("commit", "-am", "real fix", cwd=tree)
    assert run_command(publish_args("check-push", "42"), tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: the patch differs from the current PR head."
    assert not json.loads(state.read_text()).get("merges")


def test_check_push_discovers_no_pr_and_refuses_ambiguous_prs(delivery, capsys):
    _, _, tree, state, _, change = delivery
    args = publish_args("check-push")
    change(open_prs=[])
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: no open PR for this topic branch."
    pr = json.loads(state.read_text())["pr"]
    change(open_prs=[pr, {**pr, "number": 43}])
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == "Push refused: multiple open PRs match this branch; pass the intended PR number."
    change(open_prs=[{**pr, "isCrossRepository": True}])
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: no open PR for this topic branch."


def test_publication_rejects_invalid_mode_flags_before_io(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cases = [
        ("open", "42"), ("comment",), ("check-push", "--needs-base", "abcdef0"),
        ("check-push", "--needs-base", "abcdef0", "--reason", "   "),
        ("check-push", "--reason", "   "), ("comment", "0"),
    ]
    for words in cases:
        assert run_command(publish_args(*words), tmp_path) == 1
        assert capsys.readouterr().out.strip() == (
            "Publication refused: use ship open --title-file <file> --body-file <file>, "
            "ship comment <PR> --body-file <file>, or ship check-push [PR] [--needs-base <sha> --reason <text>]."
        )
    assert run_command(publish_args("42", "43"), tmp_path) == 1
    assert capsys.readouterr().out.strip() == "ship blocked: publication arguments require open, comment, or check-push."
    assert not (tmp_path / "home").exists()


def test_publication_file_and_delivery_flag_refusals(delivery, capsys):
    _, _, tree, state, _, _ = delivery
    title, body = publication_files(state)
    assert run_command(publish_args("open"), tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: --title-file is required; write the final PR title to a UTF-8 file."
    )
    args = publish_args("open", "--title-file", str(title), "--body-file", str(body))
    for content in ("", "TODO", "first line\nsecond line\n"):
        title.write_text(content)
        assert run_command(args, tree) == 1
        assert capsys.readouterr().out.strip() == (
            "Publication refused: title is empty, multiline, or a placeholder; write the final title in --title-file."
        )
    title.write_text("fix: complete title")
    assert run_command(publish_args("open", "--title-file", str(title)), tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: --body-file is required; write the complete text to a UTF-8 file."
    )
    body.write_text(" \n")
    assert run_command(publish_args("comment", "42", "--body-file", str(body)), tree) == 1
    assert capsys.readouterr().out.strip() == "Publication refused: empty body; write the complete text in --body-file."
    for flag in (("--preflight-only",), ("--smoke", "true")):
        assert run_command(publish_args("check-push", *flag), tree) == 1
        assert capsys.readouterr().out.strip() == (
            "Publication refused: --preflight-only and --smoke apply only to numeric PR delivery."
        )
    assert not json.loads(state.read_text()).get("created")
    assert not json.loads(state.read_text()).get("comments")


def test_check_push_refuses_wrong_pr_and_stale_fetched_head(delivery, capsys):
    _, _, tree, state, _, change = delivery
    args = publish_args("check-push", "42")
    original = json.loads(state.read_text())["pr"]
    for wrong in ({"state": "MERGED"}, {"baseRefName": "stack"},
                  {"headRefName": "other"}, {"isCrossRepository": True}):
        change(pr={**original, **wrong})
        assert run_command(args, tree) == 1
        assert capsys.readouterr().out.strip() == (
            "Push refused: PR must be open, target main, and match this origin topic branch."
        )
    change(pr={**original, "headRefOid": "b" * 40})
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Push refused: fetched PR head differs from GitHub; repeat check-push before pushing."
    )


def test_check_push_keeps_stable_whitespace_contract_and_binary_changes(delivery, capsys):
    _, _, tree, state, git, _ = delivery
    for key, value in (("patchid.verbatim", "true"), ("diff.context", "0"),
                       ("diff.algorithm", "histogram"), ("diff.relative", "true"),
                       ("diff.ignoreSubmodules", "all"), ("diff.noprefix", "true")):
        git("config", key, value, cwd=tree)
    original = json.loads(state.read_text())["pr"]
    (tree / "consumer.txt").write_text(" change\n")
    git("commit", "-am", "needed indentation fix", cwd=tree)
    args = publish_args("check-push", "42")
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        f"Push refused: same patch as PR head {original['headRefOid']}; main advancing does not require a new head "
        "(ship accepts it). Push only for a real conflict or a needed commit: add --needs-base <sha> with a reason."
    )
    args.needs_base = git("rev-parse", "HEAD", cwd=tree)
    args.reason = "The indentation change affects Python behavior."
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Push refused: --needs-base must name a commit SHA reachable from HEAD and origin/main."
    )
    args.needs_base = original["baseRefOid"]
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == (
        f"Push check passed: explicit base dependency {args.needs_base}; reason: {args.reason}"
    )
    (tree / "binary.dat").write_bytes(b"\x00\xff\xfe")
    git("add", "binary.dat", cwd=tree)
    git("commit", "-m", "needed binary change", cwd=tree)
    assert run_command(publish_args("check-push", "42"), tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: the patch differs from the current PR head."
