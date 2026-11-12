"""Evidence capability boundaries for the retained read-only workspace tools."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.qa import ask
from evallab.qa.trace import BUNDLE_SCHEMA, Bundle


def _bundle(tmp_path: Path) -> Bundle:
    root = tmp_path / "bundle"
    root.mkdir()
    files = {
        "prompt.md": "Return value 2.\n",
        "file_changes.diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n",
        "trajectory.json": json.dumps({"steps": [{"step_id": 0, "source": "user", "message": "Return value 2."}]}),
        "steps.txt": "[step 0] user\nReturn value 2.\n",
    }
    for name, content in files.items():
        (root / name).write_text(content)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "subject": {"kind": "trace", "trial_id": "opaque"},
        "reward": {"value": None},
        "availability": {"tests": "missing", "verifier_output": "missing"},
        "files": {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()},
    }
    (root / "bundle.json").write_text(json.dumps(manifest))
    return Bundle(root, manifest)


@pytest.mark.parametrize("tool,args", [
    ("bash", {"path": "prompt.md"}),
    ("read", {"path": "https://example.invalid"}),
    ("read", {"path": "/etc/passwd"}),
    ("read", {"path": "../prompt.md"}),
    ("read", {"path": "bundle.json"}),
    ("read", {"path": "prompt.md", "command": "curl example.invalid"}),
    ("read", {"path": "prompt.md", "start_line": True}),
    ("read", {"path": "prompt.md", "end_line": 201}),
    ("diff", {"path": "prompt.md"}),
])
def test_workspace_capability_rejects_executable_or_unsealed_access(
    tmp_path: Path, tool: str, args: dict[str, Any],
) -> None:
    bundle = _bundle(tmp_path)
    tools = ask.WorkspaceTools(bundle.root, set(bundle.manifest["files"]))
    with pytest.raises(ValueError):
        tools.run(tool, args)


def test_workspace_tools_reject_symlink_targets(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    outside = tmp_path / "secret"
    outside.write_text("must not be read")
    (bundle.root / "link.txt").symlink_to(outside)
    tools = ask.WorkspaceTools(bundle.root, {"link.txt"})
    with pytest.raises(ValueError, match="symlink"):
        tools.run("read", {"path": "link.txt"})


def test_tools_quote_lines_and_search_literals_without_regex_or_execution(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    tools = ask.WorkspaceTools(bundle.root, set(bundle.manifest["files"]))
    assert tools.run("read", {"path": "/workspace/prompt.md"}) == "prompt.md:1: Return value 2."
    assert "file_changes.diff" in tools.run("read", {"path": "."})
    assert "bundle.json" not in tools.run("read", {"path": "."})
    assert tools.run("grep", {"path": ".", "pattern": "RETURN 2"}) == "file_changes.diff:5: +return 2"
    assert tools.run("grep", {"path": ".", "pattern": ".*"}).startswith("[no matches")
    assert "file_changes.diff:5: +return 2" in tools.run("diff", {"path": "file_changes.diff"})

