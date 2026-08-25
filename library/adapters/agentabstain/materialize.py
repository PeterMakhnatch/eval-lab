"""Materialize the selected pair into ignored Harbor output deterministically."""
from __future__ import annotations

import argparse
import json
import stat
import subprocess
from pathlib import Path

from . import templates
from .adapter import MANIFEST, source_digest

ROOT = Path(__file__).parents[3]
OUTPUT_ROOT = ROOT / "derived/harbor-tasks/agentabstain"
SEED = Path(__file__).parent / "source/canary_state.json"
RUNTIME = Path(__file__).parent / "runtime.py"
MATERIALIZER_VERSION = "agentabstain-materializer/v3"


def source_id() -> str:
    template_names = (
        "COMMON", "INSTRUCTION_SUFFIX", "TOOLS", "DOCKERFILE", "ENTRYPOINT",
        "ACT_SOLUTION", "ABSTAIN_SOLUTION", "TEST_DOCKERFILE", "VERIFY",
    )
    material = {
        "materializer_version": MATERIALIZER_VERSION,
        "canary": json.loads(MANIFEST.read_text()),
        "state": json.loads(SEED.read_text()),
        "pins": json.loads((Path(__file__).parent / "source/pins.json").read_text()),
        "runtime": RUNTIME.read_bytes().hex(),
        "templates": {name: getattr(templates, name) for name in template_names},
    }
    return source_digest(material).split(":", 1)[1]

def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    if path.suffix == ".sh":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _package(root: Path, variant: dict, pair_id: str) -> None:
    task_type = variant["task_type"]
    category = pair_id.rsplit("/", 1)[0]
    category = category.replace("_", "-").replace("-specification", "")
    name = f"agentabstain-{category}-preview-002-{task_type}"
    package = root / name
    task_name = f"agentabstain/{pair_id.replace('/', '-')}-{task_type}"
    _write(package / "task.toml", templates.COMMON.format(
        name=task_name, task_type=task_type,
    ))
    _write(package / "instruction.md", variant["instruction"] + templates.INSTRUCTION_SUFFIX)
    _write(package / "environment/Dockerfile", templates.DOCKERFILE)
    _write(package / "environment/TOOLS.md", templates.TOOLS)
    _write(package / "environment/entrypoint.sh", templates.ENTRYPOINT)
    _write(package / "environment/initial_state.json", SEED.read_bytes())
    _write(package / "environment/runtime.py", RUNTIME.read_bytes())
    solution = templates.ACT_SOLUTION if task_type == "act" else templates.ABSTAIN_SOLUTION
    _write(package / "solution/solve.sh", solution)
    _write(package / "tests/Dockerfile", templates.TEST_DOCKERFILE)
    test_script = (
        "#!/bin/sh\nset -eu\n"
        f"AGENTABSTAIN_TASK_TYPE={task_type} python3 /app/verify.py\n"
    )
    _write(package / "tests/test.sh", test_script)
    _write(package / "tests/verify.py", templates.VERIFY)
    _write(package / "tests/fixtures/initial_state.json", SEED.read_bytes())
    _write(package / "workbench/nop.sh", "#!/bin/sh\nset -eu\nexit 0\n")
    _write(
        package / "workbench/mutant.sh",
        "#!/bin/sh\nset -eu\n"
        "python3 /app/runtime.py call spotify.write_gmail_draft "
        "'{\"action\":\"update\",\"draft_id\":\"draft_katie_001\",\"body\":\"mutant\"}'\n",
    )


def materialize(output_root: Path = OUTPUT_ROOT) -> Path:
    output = output_root / source_id()
    if output.exists():
        for path in sorted(output.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for variant in data["variants"]:
        _package(output, variant, data["pair_id"])
    _write(output / "SOURCE_DIGEST", source_id() + "\n")
    return output


def assert_no_committed_generated(repo_root: Path = ROOT) -> None:
    tracked = subprocess.check_output(["git", "-C", str(repo_root), "ls-files", "derived/harbor-tasks"], text=True).splitlines()
    if tracked:
        raise RuntimeError("generated Harbor corpus is committed: " + ", ".join(tracked))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--check-no-committed-generated", action="store_true")
    args = parser.parse_args()
    if args.check_no_committed_generated:
        assert_no_committed_generated()
    print(materialize(args.output_root))


if __name__ == "__main__":
    main()
