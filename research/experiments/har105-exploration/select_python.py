#!/usr/bin/env python3
"""HAR-105 part 3: 10 train-side Python code tasks from different repos, light images first.

Peter (2026-09-30 about 02:30Z) narrowed exploration to one niche: MiMo Python
code tasks (hidden-test graders, no model judge). Rule:

1. Pool: sealed-split ``train`` code tasks with ``metadata.category == "Python"``
   whose hidden test patch touches a ``.py`` file. Drop ``error`` findings, the
   pinned export-broken list and the HAR-105 suspects, as in ``select.py``.
2. Repo: the split's ``split_group`` when it names a repo; otherwise the
   project package the hidden tests import most (``repo_key``). No two picks
   share a split group or a repo key.
3. Rank by ``sha256("har105-py:" + task_id)`` and take the first ``SHORTLIST``
   tasks with distinct repos. Of those, the ``CANDIDATES`` with the smallest
   compressed image (Docker Hub manifest layer sizes, cached in
   ``image_sizes.json``) get a Daytona nop.
4. The set is the first ``PICKS`` candidates, lightest first, whose nop is
   ``ok`` with reward 0 and shows no setup error (``nopspec.SETUP_ERROR``),
   unless ``setup_error_ok`` records why a match is the agent's missing work.

Writes ``python_selection.json`` and one nop spec per candidate in ``specs/``.
"""

from __future__ import annotations

import collections
import hashlib
import json
import re
import sys
import tomllib
import urllib.request

import pyarrow.parquet as pq
from nopspec import (
    OUT,
    PRIMARY,
    ROOT,
    SETUP_ERROR,
    SNAPSHOTS,
    SUSPECTS,
    nop_spec,
    nop_verifier_text,
)

SEED = "har105-py:"
PICKS = 10
SHORTLIST = 30
CANDIDATES = 13
SIZES = OUT / "image_sizes.json"
#: Non-project modules a hidden test may import; the first remaining import is
#: the project under test.
NOT_PROJECT = set(sys.stdlib_module_names) | {
    "pytest",
    "mock",
    "hypothesis",
    "freezegun",
    "responses",
    "numpy",
    "pandas",
    "tests",
    "test",
    "conftest",
    "unittest",
    "six",
    "requests",
    "yaml",
    "attr",
    "attrs",
    "pydantic",
    "sqlalchemy",
    "django",
    "flask",
    "typing_extensions",
}
IMPORT = re.compile(r"^\+\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.M)
PATCHED = re.compile(r"^diff --git a/(\S+)", re.M)


#: Nops whose SETUP_ERROR match is the missing feature itself, checked by hand.
SETUP_ERROR_OK = {
    "format-code-task-001832": "the collection error is `cannot import name 'rename' from 'siuba'`, "
    "the verb the instruction asks for",
}
#: The project each candidate tests, read from its test paths and nop traceback.
#: ``split_group`` is wrong for two: 002401 is pyproj (not user-attachments/assets)
#: and 002864 is sqlglot (not apache/superset).
REPO = {
    "format-code-task-000226": "Pylons/waitress",
    "format-code-task-000383": "quickfix (Python FIX client)",
    "format-code-task-000738": "cupy/cupy",
    "format-code-task-000927": "facelessuser/soupsieve",
    "format-code-task-001832": "machow/siuba",
    "format-code-task-001896": "meyt/linkpreview",
    "format-code-task-002218": "pandas-dev/pandas",
    "format-code-task-002256": "peter-wangxu/persist-queue",
    "format-code-task-002259": "PHAREHUB/PHARE",
    "format-code-task-002391": "pypa/pip-audit",
    "format-code-task-002401": "pyproj4/pyproj",
    "format-code-task-002407": "python-control/python-control",
    "format-code-task-002864": "tobymao/sqlglot",
}


def rank(task_id: str) -> str:
    return hashlib.sha256((SEED + task_id).encode()).hexdigest()


def repo_key(split_group: str, patch: str) -> str:
    if not split_group.startswith("code:format-code-task-"):
        return split_group
    imports = collections.Counter(
        m.split(".")[0] for m in IMPORT.findall(patch) if m.split(".")[0] not in NOT_PROJECT
    )
    if imports:
        return "pkg:" + imports.most_common(1)[0][0]
    paths = [p for p in PATCHED.findall(patch) if p.endswith(".py")]
    return "path:" + paths[0].split("/")[0] if paths else split_group


def image_mb(image: str, sizes: dict[str, float]) -> float:
    """Compressed image size in MB from the registry manifest, cached by digest."""
    if image not in sizes:
        name, digest = image.removeprefix("docker.io/").split("@")
        token = json.load(
            urllib.request.urlopen(
                "https://auth.docker.io/token?service=registry.docker.io"
                f"&scope=repository:{name}:pull"
            )
        )["token"]
        request = urllib.request.Request(
            f"https://registry-1.docker.io/v2/{name}/manifests/{digest}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.docker.distribution.manifest.v2+json,"
                "application/vnd.oci.image.manifest.v1+json",
            },
        )
        manifest = json.load(urllib.request.urlopen(request))
        sizes[image] = round(sum(layer["size"] for layer in manifest["layers"]) / 1e6, 1)
    return sizes[image]


def main() -> None:
    split = json.loads((ROOT / "research/experiments/har81-mimo-sft/split.json").read_text())
    cohort = json.loads((ROOT / "research/experiments/har81-mimo-sft/cohort.json").read_text())
    broken = set(cohort["excluded_task_version_digests"])
    catalog = PRIMARY / "derived/parquet/external/task_catalog"
    errors = {
        row["task_version_digest"]
        for row in pq.read_table(catalog / "task_findings.parquet").to_pylist()
        if row["severity"] == "error"
    }
    nops = {
        row["task_version_digest"]: row
        for row in pq.read_table(catalog / "task_qualification.parquet").to_pylist()
        if row["backend"] == "daytona"
    }
    sizes = json.loads(SIZES.read_text()) if SIZES.exists() else {}

    pool = sorted(
        (t for t in split["tasks"] if t["domain"] == "code" and t["split"] == "train"),
        key=lambda t: rank(t["task_id"]),
    )
    groups: set[str] = set()
    repos: set[str] = set()
    shortlist: list[dict] = []
    for task in pool:
        digest = task["task_version_digest"]
        if digest in errors or digest in broken or task["task_id"] in SUSPECTS:
            continue
        rel = f"derived/task-store/hf/{SNAPSHOTS['code']}/tasks/{task['task_id']}"
        path = PRIMARY / rel
        config = tomllib.loads((path / "task.toml").read_text())
        if config["metadata"].get("category") != "Python":
            continue
        patch = (path / "tests/test.patch").read_text(errors="replace")
        if not any(p.endswith(".py") for p in PATCHED.findall(patch)):
            continue
        repo = repo_key(task["split_group"], patch)
        if task["split_group"] in groups or repo in repos:
            continue
        groups.add(task["split_group"])
        repos.add(repo)
        shortlist.append(
            {
                "task_id": task["task_id"],
                "title": config["metadata"].get("title"),
                "split_group": task["split_group"],
                "repo_key": repo,
                "task_version_digest": digest,
                "rank": rank(task["task_id"])[:12],
                "task": rel,
                "image_mb": image_mb(config["environment"]["docker_image"], sizes),
            }
        )
        if len(shortlist) == SHORTLIST:
            break
    SIZES.write_text(json.dumps(dict(sorted(sizes.items())), indent=2) + "\n")

    candidates = sorted(shortlist, key=lambda e: (e["image_mb"], e["rank"]))[:CANDIDATES]
    specs = OUT / "specs"
    for old in specs.glob("har105-qual-py-*.json"):
        old.unlink()
    chosen = 0
    for entry in candidates:
        name = f"har105-qual-py-{entry['task_id'].removeprefix('format-code-task-')}"
        spec = nop_spec(
            entry["task"],
            name,
            "The Python code task starts, passes its healthcheck and grades on Daytona: "
            "a nop control completes the verifier with reward 0 and no setup error.",
        )
        (specs / f"{name}.json").write_text(json.dumps(spec, indent=2) + "\n")
        entry["spec"] = f"specs/{name}.json"
        nop = nops.get(entry["task_version_digest"])
        if nop is None:
            entry["nop"] = {"status": "pending"}
        else:
            text = nop_verifier_text(nop["job_name"]) or ""
            match = SETUP_ERROR.search(text)
            clean = (
                nop["status"] == "ok"
                and nop["reward"] == 0
                and (match is None or entry["task_id"] in SETUP_ERROR_OK)
            )
            entry["nop"] = {
                "status": "clean" if clean else "unclean",
                "qualification": nop["status"],
                "job_name": nop["job_name"],
                "reward": nop["reward"],
                "setup_error": match.group(0) if match else None,
                "setup_error_ok": SETUP_ERROR_OK.get(entry["task_id"]),
                "est_cost_usd": nop["est_cost_usd"],
            }
        entry["repo"] = REPO.get(entry["task_id"])
        entry["in_set"] = entry["nop"]["status"] == "clean" and chosen < PICKS
        chosen += entry["in_set"]

    (OUT / "python_selection.json").write_text(
        json.dumps(
            {
                "card": "HAR-105 part 3",
                "seed": SEED,
                "split_manifest_digest": split["manifest_digest"],
                "picks": PICKS,
                "shortlist": SHORTLIST,
                "candidates": CANDIDATES,
                "in_set": chosen,
                "selection": candidates,
                "not_nop_checked": sorted(
                    (e for e in shortlist if e not in candidates), key=lambda e: e["rank"]
                ),
            },
            indent=2,
        )
        + "\n"
    )
    for e in candidates:
        print(e["task_id"], e.get("repo"), e["image_mb"], e["nop"]["status"], e.get("in_set"))


if __name__ == "__main__":
    main()
