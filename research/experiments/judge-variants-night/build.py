"""Reproduce the $0 judge-domain census and variant fleet.

Run with --census, then --derive. Public dataset GETs only; no model API,
Docker, or inference calls. Workspace cache and downloaded source data stay
outside Git. Snapshot packages are never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evallab import (
    general_nop_gate as g3,
)
from evallab import (
    general_pinned_backup as g2,
)
from evallab import (
    general_strict_answer as g4,
)
from evallab import (
    webdev_brief_explicit as w1,
)
from evallab import (
    webdev_structural_gate as w3,
)
from evallab import (
    webdev_temp0_pin as w2,
)
from evallab.task_variants import VariantExistsError, default_variants_root

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCRATCH = Path("/private/tmp/mimo-night/judge-variants")
SHARED = REPO.parent.parent
GENERAL = SHARED / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-general@10b732c5079c"
WEBDEV = SHARED / "derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-webdev@e1a6293376e8"


def source(snapshot: Path, task: str) -> dict:
    provenance = json.loads((snapshot / "provenance.json").read_text())
    return {"kind": "hf", "repo": provenance["source_uri"].split("datasets/")[1],
            "revision": provenance["revision"], "path": f"tasks/{task}"}


def read_meta(task: Path) -> dict:
    return json.loads((task / g3.META_REL).read_text())


def get_file(snapshot: dict, entry: dict, dest: Path) -> None:
    """Verify cached bytes too; a timeout/HTTP error is never a zero score."""
    def valid(data: bytes) -> bool:
        if len(data) != entry["size"]:
            return False
        if "sha256" in entry:
            return hashlib.sha256(data).hexdigest() == entry["sha256"]
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == entry["git_sha1"]

    if dest.is_file() and valid(dest.read_bytes()):
        return
    url = (f'https://huggingface.co/datasets/{snapshot["dataset"]}/resolve/'
           f'{snapshot["revision"]}/{urllib.parse.quote(entry["path"])}')
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            if not valid(data):
                raise ValueError(f"hash mismatch: {entry['path']}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return
        except OSError:
            if attempt == 5:
                raise
            time.sleep(2 ** (attempt + 1))


def workplace(task: Path) -> Path:
    root = SCRATCH / "workplaces" / task.name
    manifest = json.loads((task / "environment/setup/files/fetch.json").read_text())
    # Rule evaluation needs state databases and submitted/source documents,
    # not agent executables or MCP scripts. Preserve their exact path layout.
    for entry in manifest["files"]:
        target = entry["target"]
        if target.startswith("/work/workspace/") or (
            target.startswith("/work/system/") and target.endswith("/state.db")
        ):
            get_file(manifest, entry, root / target.removeprefix("/work/"))
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    return root


def run_rule(task: Path, ws: Path, fn: str) -> dict:
    meta = read_meta(task)
    code = meta.get("check_code", "")
    helpers = (task / "tests/verifier/_helpers.py").read_text()
    if "import random" in code or "random." in code:
        return {"score": 0.0, "detail": "random disabled by upstream"}
    raw = ("from pathlib import Path\n" + helpers + "\n" + code + "\n"
           "import json as _j, sys as _s\n"
           f"print('___R___' + _j.dumps({fn}(Path(_s.argv[1])), ensure_ascii=False, default=str))\n")
    lines = raw.splitlines()
    future = list(dict.fromkeys(line for line in lines if line.lstrip().startswith("from __future__ import")))
    rest = [line for line in lines if not line.lstrip().startswith("from __future__ import")]
    with tempfile.TemporaryDirectory(prefix="mimo-rule-") as tmp:
        script = Path(tmp) / "rule.py"
        script.write_text("\n".join(future + rest))
        proc = subprocess.run([sys.executable, str(script), str(ws)],
                              capture_output=True, text=True, timeout=300)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("___R___"):
            return json.loads(line.removeprefix("___R___"))
    # Keep infrastructure/runtime failures separate from semantic negative controls.
    return {"score": 0.0, "detail": proc.stderr[-400:], "execution_error": True}


def census_task(task: Path) -> dict:
    meta = read_meta(task)
    rules = [it for it in meta["items"] if it.get("method") == "rule"]
    if not rules:
        return {"task": task.name, "evidence_tier": "MEASURED-static-no-rules",
                "floor_pre": 0.0, "floor_g2": 0.0, "floor_g3": 0.0,
                "zeroed_ids": [], "pre": {}, "post": {}, "execution_errors": []}
    root = workplace(task)
    for path in (root / "system").rglob("state.db.pinned_backup"):
        path.unlink()  # Only our scratch replicas, never dataset/sibling material.
    pre = {it["id"]: run_rule(task, root / "workspace", it["fn"]) for it in rules}
    for db in (root / "system").rglob("state.db"):
        with (
            sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as source_db,
            sqlite3.connect(db.with_name("state.db.pinned_backup")) as snapshot_db,
        ):
            source_db.backup(snapshot_db)
    post = {it["id"]: run_rule(task, root / "workspace", it["fn"]) for it in rules}
    scores = {item_id: float(result.get("score", 0)) for item_id, result in post.items()}
    passing = {it["id"]: scores[it["id"]] for it in rules
               if not it.get("gate") and float(it.get("weight", 1)) > 0
               and scores[it["id"]] > 0 and not post[it["id"]].get("execution_error")}
    updated = g3.build_meta(meta, passing) if passing else meta
    errors = [f"{stage}:{item_id}" for stage, results in (("pre", pre), ("post", post))
              for item_id, result in results.items() if result.get("execution_error")]
    return {"task": task.name, "evidence_tier": "MEASURED-rule-subprocess",
            "floor_pre": g3.rule_floor(meta, {k: float(v.get("score", 0)) for k, v in pre.items()}),
            "floor_g2": g3.rule_floor(meta, scores), "floor_g3": g3.rule_floor(updated, scores),
            "zeroed_ids": list(passing), "pre": pre, "post": post, "execution_errors": errors}


def census() -> None:
    output = HERE / "rule-census.jsonl"
    prior = {}
    if output.exists():
        prior = {r["task"]: r for r in (json.loads(line) for line in output.read_text().splitlines())}
    tasks = sorted(t for t in (GENERAL / "tasks").iterdir() if t.is_dir())
    pending = [task for task in tasks if task.name not in prior or "fetch_error" in prior[task.name]
               or any("ModuleNotFoundError" in r.get("detail", "")
                      for r in prior[task.name].get("post", {}).values())]
    def safe(task: Path) -> dict:
        try:
            return census_task(task)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {"task": task.name, "fetch_error": str(exc)}
    with ThreadPoolExecutor(max_workers=2) as executor:
        for result in executor.map(safe, pending):
            prior[result["task"]] = result
            # Atomic complete output; resumability does not require a process watcher.
            output.write_text("".join(json.dumps(prior[k], ensure_ascii=False) + "\n" for k in sorted(prior)))
            print(result["task"], {k: v for k, v in result.items() if k in ("floor_pre", "floor_g2", "floor_g3", "fetch_error")}, flush=True)


def derive() -> None:
    census_path = HERE / "rule-census.jsonl"
    results = {r["task"]: r for r in (json.loads(line) for line in census_path.read_text().splitlines())}
    if len(results) != 925 or any("fetch_error" in r for r in results.values()):
        raise ValueError("complete successful 925-task census required before derivation")
    packages = []
    checked_scripts = set()
    python_files = 0
    for snapshot in (GENERAL, WEBDEV):
        for task in sorted(t for t in (snapshot / "tasks").iterdir() if t.is_dir()):
            cur = task
            parent_source = source(snapshot, task.name)
            transforms = []
            if snapshot == GENERAL:
                meta = read_meta(task)
                if "pinned_backup" in meta.get("check_code", ""):
                    transforms.append((g2.derive_general_pinned_backup, {}))
                transforms.append((g4.derive_general_strict_answer, {}))
                row = results[task.name]
                scores = {item_id: float(row["post"][item_id]["score"]) for item_id in row["zeroed_ids"]}
                if scores:
                    transforms.append((g3.derive_general_nop_gate,
                                       {"scores": scores, "evidence": "research/experiments/judge-variants-night/rule-census.jsonl"}))
            else:
                transforms = [(w2.derive_webdev_temp0_pin, {}), (w3.derive_webdev_structural_gate, {})]
                cfg = json.loads((task / "tests/grade.json").read_text())
                if len(cfg["query"]) > w1.QUERY_CAP:
                    transforms.append((w1.derive_webdev_brief_explicit, {}))
            chain = []
            for fn, kwargs in transforms:
                try:
                    rec = fn(cur, repo_root=REPO, parent_source=parent_source,
                             created_by="judge-variants-night", **kwargs)
                    digest = rec.variant_digest
                    task_name = rec.task_name
                except VariantExistsError as exc:
                    # VariantExistsError reports the exact content-addressed record.
                    prefix = "lineage record already exists: "
                    if not str(exc).startswith(prefix):
                        raise
                    data = json.loads(Path(str(exc).removeprefix(prefix)).read_text())
                    digest, task_name = data["variant_digest"], data["task_name"]
                slug = task_name.replace("/", "__")
                record = Path("library/task-variants") / slug / (digest.removeprefix("sha256:")[:12] + ".json")
                cur = default_variants_root(REPO) / slug / digest.removeprefix("sha256:")[:12]
                parent_source = {"kind": "variant", "record": str(record)}
                chain.append(str(record))
            for script in sorted((cur / "tests").glob("*.py")):
                data = script.read_bytes()
                script_sha = hashlib.sha256(data).hexdigest()
                if script_sha not in checked_scripts:
                    compile(data, str(script), "exec")
                    checked_scripts.add(script_sha)
                python_files += 1
            packages.append({"domain": "general" if snapshot == GENERAL else "webdev",
                             "task": task.name, "record": str(record), "package_digest": digest,
                             "chain": chain})
    (HERE / "packages.jsonl").write_text("".join(json.dumps(row) + "\n" for row in packages))
    (HERE / "package-validation.json").write_text(json.dumps({
        "evidence_tier": "MEASURED",
        "command": "uv run python research/experiments/judge-variants-night/build.py --derive",
        "packages": len(packages),
        "python_files": python_files,
        "unique_python_sources_compiled": len(checked_scripts),
        "model_calls": 0,
        "syntax_errors": 0,
    }, indent=2) + "\n")
    print("final packages", len(packages), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", action="store_true")
    parser.add_argument("--derive", action="store_true")
    args = parser.parse_args()
    if args.census:
        census()
    if args.derive:
        derive()


if __name__ == "__main__":
    main()
