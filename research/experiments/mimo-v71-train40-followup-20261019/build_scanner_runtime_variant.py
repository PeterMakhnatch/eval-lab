"""Bake the scanner's missing dependency before any agent starts.

The frozen train40 task and its tests stay immutable. This producer derives a
new environment-only candidate; its parent's controls do not transfer to it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evallab.registry import task_directory_digest
from evallab.task_variants import derive_task, verify

PARENT_DIGEST = "sha256:9ef93696066f76fe2352a39ece1f6cac9001b6ebb772127844cf5f548a66e666"
IMAGE = (
    "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:"
    "5500f28f54d7b1440f51ccf8fde50ae3b9b278c1283894effe9c559474904f4a"
)
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/80/c5/"
    "0c06759b95747882bb50abda18f5fb48c3e9b0fbfc6ebc0e23550b52415d/"
    "stevedore-5.5.0-py3-none-any.whl"
)
WHEEL_SHA256 = "18363d4d268181e8e8452e71a38cd77630f345b2ef6b4a8d5614dac5ee0d18cf"
PARENT_RECORD = (
    "library/task-variants/mimo-v2.6-rl__candidate-2684-security-appsec/"
    "9ef93696066f.json"
)


def build(parent: Path, repo_root: Path) -> dict[str, object]:
    """Derive the pinned candidate without installing or executing task code."""
    if task_directory_digest(parent) != PARENT_DIGEST:
        raise ValueError("parent is not the frozen train40 scanner package")
    config = (parent / "task.toml").read_text()
    image_declaration = f'docker_image = "{IMAGE}"\n'
    if config.count(image_declaration) != 1:
        raise ValueError("expected exactly one pinned scanner image declaration")
    dockerfile = (parent / "environment/Dockerfile").read_text().replace(
        " (the same as [environment].docker_image)", ""
    )
    dockerfile += (
        "\n# Author-time dependency repair; no agent-side dependency installation.\n"
        "RUN /usr/local/bin/python3.12 -m pip install --disable-pip-version-check \\\n"
        "    --no-cache-dir --no-deps \\\n"
        f"    '{WHEEL_URL}#sha256={WHEEL_SHA256}'\n"
    )
    record = derive_task(
        parent,
        changes={
            "task.toml": config.replace(image_declaration, "").encode(),
            "environment/Dockerfile": dockerfile.encode(),
        },
        transform="scanner-runtime-dependency@1",
        rationale=(
            "Bake the missing generic stevedore dependency in the task's pinned "
            "Python 3.12 image before agent setup; do not provide reference code "
            "to nop, change tests, or permit agent network/dependency installs."
        ),
        created_by="mimo-data",
        inputs={
            "parent_package_digest": PARENT_DIGEST,
            "base_image": IMAGE,
            "dependency": "stevedore==5.5.0",
            "wheel_url": WHEEL_URL,
            "wheel_sha256": WHEEL_SHA256,
            "dependency_python": "/usr/local/bin/python3.12",
            "qualification_or_admission": False,
        },
        parent_source={"kind": "variant", "record": PARENT_RECORD},
        repo_root=repo_root,
    )
    failures = verify(record, parent_dir=parent, repo_root=repo_root)
    if failures:
        raise ValueError(f"variant replay verification failed: {failures}")
    return record.model_dump(mode="json", by_alias=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.parent.resolve(), args.repo_root.resolve()), indent=2))


if __name__ == "__main__":
    main()
