"""Auto-locate a trial's task metadata.json (shared by judge.py and detect.py).

Chain, first hit wins:
  1. explicit --metadata path;
  2. the trial config's staged task path (<path>/metadata.json), when the
     stage still exists;
  3. the job's experiment-spec.json `task` field (repo-relative package path);
  4. the trial result.json `task_name` tail matched against
     library/benchmarks/cheatbench/*/tasks/*/metadata.json by instance_id
     (or task-dir name).

Never guesses across tasks: step 4 only fires on an exact
instance_id/dir-name match, otherwise it raises with a clear message.
"""

from __future__ import annotations

import json
from pathlib import Path

JUDGE_DIR = Path(__file__).resolve().parent


def find_repo_root(starts: list[str | Path]) -> Path | None:
    """Nearest ancestor containing library/benchmarks/cheatbench."""
    for s in starts:
        p = Path(s).resolve() if str(s) else Path.cwd()
        for cand in [p, *p.parents]:
            if (cand / "library" / "benchmarks" / "cheatbench").is_dir():
                return cand
    return None


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _job_dir(trial_dir: Path) -> Path | None:
    cfg = _read_json(trial_dir / "config.json") or {}
    for key in ("trials_dir",):
        if cfg.get(key):
            return Path(cfg[key])
    if (trial_dir.parent / "experiment-spec.json").exists():
        return trial_dir.parent
    return None


def _match_by_instance(bench_root: Path, tail: str) -> Path | None:
    """Exact instance_id or task-dir match. No fuzzy guessing."""
    if not tail:
        return None
    hits = []
    for meta_path in sorted(bench_root.glob("*/tasks/*/metadata.json")):
        meta = _read_json(meta_path)
        if not meta:
            continue
        if (meta.get("instance_id") == tail or meta_path.parent.name == tail
                or meta_path.parent.name.endswith("-" + tail)
                or meta_path.parent.name.endswith("_" + tail)):
            hits.append(meta_path)
    if len(hits) == 1:
        return hits[0]
    return None  # zero or ambiguous: refuse to guess


def locate_metadata(trial_dir: str | Path,
                    explicit: str | None = None) -> tuple[dict, str, Path | None]:
    """Return (metadata, source_description, package_dir_or_None).

    Raises SystemExit with an actionable message when nothing resolves.
    """
    trial_dir = Path(trial_dir)
    if explicit:
        meta = _read_json(Path(explicit))
        if meta is None:
            raise SystemExit(f"--metadata {explicit} is unreadable")
        pkg = Path(explicit).parent
        return meta, f"flag:{explicit}", pkg if (pkg / "task.toml").exists() else None

    # 2. staged task path from the trial config.
    cfg = _read_json(trial_dir / "config.json") or {}
    task_path = (cfg.get("task") or {}).get("path")
    if task_path and (Path(task_path) / "metadata.json").exists():
        meta = _read_json(Path(task_path) / "metadata.json") or {}
        return meta, f"staged:{task_path}", Path(task_path)

    root = find_repo_root([trial_dir, Path.cwd()])

    # 3. job experiment-spec.json `task` (repo-relative package path).
    job = _job_dir(trial_dir)
    if job is not None and root is not None:
        spec = _read_json(job / "experiment-spec.json") or {}
        task_rel = spec.get("task")
        if task_rel and (root / task_rel / "metadata.json").exists():
            meta = _read_json(root / task_rel / "metadata.json") or {}
            return meta, f"experiment-spec:{task_rel}", root / task_rel

    # 4. trial result.json task_name tail matched by instance_id.
    if root is not None:
        res = _read_json(trial_dir / "result.json") or {}
        tail = str(res.get("task_name") or "").rsplit("/", 1)[-1]
        hit = _match_by_instance(root / "library" / "benchmarks" / "cheatbench", tail)
        if hit is not None:
            return _read_json(hit) or {}, f"task-name:{tail}", hit.parent

    raise SystemExit(
        f"no metadata.json found for trial {trial_dir} "
        f"(staged path {task_path!r} gone, no experiment-spec/job match); "
        f"pass --metadata explicitly")
