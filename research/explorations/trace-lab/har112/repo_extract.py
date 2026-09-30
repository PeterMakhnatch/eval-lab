"""Extract each task's repository (/workspace/repo in the pinned task image) straight from Docker Hub.

No Docker daemon, no sandbox spend: each image layer is streamed once from the registry, and only small text
files under workspace/repo/ are written; .git and binaries are skipped. Output per task:
    CACHE/<task_id>/repo/...        source tree at the task's base state
    CACHE/<task_id>/extract.json    image digest, layers, bytes streamed, files kept

Usage:
    python3 repo_extract.py --cache DIR --workers 6 TASK_DIR [TASK_DIR ...]
    python3 repo_extract.py --cache DIR --list FILE

Resumable: tasks with an extract.json are skipped. Later layers override earlier ones; OCI whiteouts
(.wh.<name>) delete the file.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REGISTRY = "https://registry-1.docker.io/v2"
PREFIXES = ("workspace/repo/", "testbed/")
MAX_FILE = 1_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".tox", ".venv", ".mypy_cache", ".pytest_cache"}
ACCEPT = ",".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])


class _Counting:
    def __init__(self, raw):
        self.raw, self.n = raw, 0

    def read(self, size=-1):
        data = self.raw.read(size)
        self.n += len(data)
        return data


def _token(repo: str) -> str:
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    return json.load(urllib.request.urlopen(url, timeout=60))["token"]


def _get(url: str, token: str, accept: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=900)


def _manifest(repo: str, ref: str, token: str) -> dict:
    m = json.load(_get(f"{REGISTRY}/{repo}/manifests/{ref}", token, ACCEPT))
    if "manifests" in m:
        ref = next(x for x in m["manifests"] if x.get("platform", {}).get("architecture") == "amd64")["digest"]
        m = json.load(_get(f"{REGISTRY}/{repo}/manifests/{ref}", token, ACCEPT))
    return m


def _image(task_dir: Path) -> tuple[str, str]:
    image = tomllib.loads((task_dir / "task.toml").read_text())["environment"]["docker_image"]
    name, digest = image.removeprefix("docker.io/").split("@", 1)
    return name, digest


def extract(task_dir: Path, cache: Path) -> dict:
    out = cache / task_dir.name
    meta_path = out / "extract.json"
    if meta_path.is_file():
        return json.loads(meta_path.read_text())
    repo_root = out / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    name, digest = _image(task_dir)
    # code tasks keep the repo at /workspace/repo; terminal tasks (graded by tests/test_outputs.py) work in /app
    prefixes = ("app/",) if (task_dir / "tests" / "test_outputs.py").is_file() else PREFIXES
    started = time.time()
    token = _token(name)
    layers = _manifest(name, digest, token)["layers"]
    streamed, kept, prefix_seen = 0, 0, set()
    for layer in layers:
        for attempt in range(3):
            try:
                if time.time() - started > 240:
                    token = _token(name)  # registry tokens expire after ~5 minutes
                resp = _Counting(_get(f"{REGISTRY}/{name}/blobs/{layer['digest']}", token))
                with tarfile.open(fileobj=resp, mode="r|*") as tar:
                    for member in tar:
                        path = member.name.removeprefix("./")
                        prefix = next((p for p in prefixes if path.startswith(p)), None)
                        if prefix is None:
                            continue
                        prefix_seen.add(prefix)
                        rel = path[len(prefix):]
                        parts = rel.split("/")
                        if not rel or SKIP_DIRS.intersection(parts):
                            continue
                        target = repo_root / rel
                        if parts[-1].startswith(".wh."):
                            (target.parent / parts[-1][4:]).unlink(missing_ok=True)
                            continue
                        if not member.isfile() or member.size > MAX_FILE:
                            continue
                        data = tar.extractfile(member).read()
                        if b"\0" in data[:4096]:
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        kept += 1
                streamed += resp.n
                break
            except (urllib.error.URLError, TimeoutError, tarfile.ReadError, ConnectionError) as exc:
                if attempt == 2:
                    raise RuntimeError(f"layer {layer['digest'][:19]}: {exc}") from exc
                time.sleep(5 * (attempt + 1))
    meta = {
        "task_id": task_dir.name,
        "image": f"{name}@{digest}",
        "layers": [{"digest": layer["digest"], "size": layer["size"]} for layer in layers],
        "bytes_streamed": streamed,
        "files_kept": kept,
        "repo_prefixes_seen": sorted(prefix_seen),
        "seconds": round(time.time() - started, 1),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--list", type=Path)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("tasks", nargs="*", type=Path)
    a = ap.parse_args()
    tasks = list(a.tasks) + ([Path(x.strip()) for x in a.list.read_text().splitlines() if x.strip()] if a.list else [])
    done = 0
    with ThreadPoolExecutor(a.workers) as pool:
        futs = {pool.submit(extract, t, a.cache): t for t in tasks}
        for fut in as_completed(futs):
            t = futs[fut]
            done += 1
            try:
                m = fut.result()
                print(f"{done}/{len(tasks)} {t.name} files={m['files_kept']} "
                      f"streamed={m['bytes_streamed'] // 2**20}MiB {m['seconds']}s", flush=True)
            except Exception as exc:  # a rerun retries it
                print(f"{done}/{len(tasks)} ERROR {t.name}: {exc}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
