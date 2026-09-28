#!/usr/bin/env python3
"""MiMo cloud nop qualification cohort (Daytona), deterministic.

Cohort (113 tasks):
* terminal: all 64, graded 3x in place by RepeatVerifier (``verifier_repeat_n``);
* code: 32 = the smallest and largest pinned image, plus 30 seats apportioned
  to languages (task.toml ``metadata.category``) by largest remainder over the
  full 2,698-task pool;
* cyber: 16 = the smallest and largest pinned image, plus one task from each
  of the 14 most common remaining projects (``metadata.category``);
* music: ``music-gk-0000``, the task that failed locally (rc=127, no /app).

Within a stratum, tasks are taken in ``sha256("har88:" + task_id)`` order.
Image sizes come from ``image-sizes.json`` (Docker Hub compressed sizes keyed
by the pinned digest; code tasks whose pinned digest is not tagged on Docker Hub
have no size and are never picked as an extreme).

Pool: the full pinned snapshots in the shared task store
(``evallab tasks pull-hf``). Output: ``cohort.json`` and one queue spec per
task in ``specs/``. Spec ``task`` paths are repo-relative and resolve against
the checkout that runs ``evallab submit``/``tick`` (``stage.sh pull`` puts
the tasks there).
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from evallab.registry import compute_task_digests, harbor_task_digest  # noqa: E402
from evallab.task_catalog import task_store_root  # noqa: E402

OUT = ROOT / "research/experiments/mimo-daytona-nop"
SEED = "har88:"
N_CODE, N_CYBER = 32, 16
REPEAT_N = 3
#: Trial overhead beyond Harbor's own phase timeouts (sandbox create/delete,
#: uploads, log download). ``timeout_seconds`` is also the executor's per-trial
#: watchdog, so without a margin a trial that uses its full phase budgets would
#: be killed as a false failure.
MARGIN_S = 300
#: Daytona's per-sandbox disk maximum (daytona.io/docs/en/sandboxes). Tasks that
#: leave ``storage_mb`` unset would get Daytona's 3 GiB default, which most code
#: images exceed once unpacked (see unpacked-sizes.json), so they request the max.
DAYTONA_MAX_DISK_MB = 10240
PINS = {
    "terminal": ("FineEnvs", "MiMo-V2.6-RL-harbor-terminal", "fe1c2b665aae1ba7a09a270d979724d32269ae6a"),
    "code": ("FineEnvs", "MiMo-V2.6-RL-harbor-code", "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785"),
    "cyber": ("FineEnvs", "MiMo-V2.6-RL-harbor-cyber", "763882ade5fc018892f1aa3c559f997138eb92cc"),
    "music": ("FineEnvs", "MiMo-V2.6-RL-harbor-music", "e1a66d4553ee20b26c571de1bc2f4193d4a32c3c"),
}
MUSIC_TASK = "music-gk-0000"
PREFIX = {"terminal": "t", "code": "c", "cyber": "y", "music": "m"}


def snapshot_rel(domain: str) -> str:
    org, repo, rev = PINS[domain]
    return f"derived/task-store/hf/{org}__{repo}@{rev[:12]}"


def order_key(task_id: str) -> str:
    return hashlib.sha256((SEED + task_id).encode()).hexdigest()


def load_pool(domain: str, store: Path, sizes: dict[str, int]) -> list[dict]:
    tasks_dir = store / snapshot_rel(domain).removeprefix("derived/task-store/") / "tasks"
    rows = []
    for task_dir in sorted(tasks_dir.iterdir()):
        toml_path = task_dir / "task.toml"
        if not toml_path.is_file():
            continue
        config = tomllib.loads(toml_path.read_text())
        env = config.get("environment", {})
        image = env.get("docker_image") or ""
        digest12 = image.split("@sha256:")[1][:12] if "@sha256:" in image else None
        healthcheck = env.get("healthcheck", {})
        rows.append({
            "task_id": task_dir.name,
            "src": task_dir,
            "category": config.get("metadata", {}).get("category") or "Unknown",
            "image_mib": sizes.get(digest12) if digest12 else None,
            "cpus": env.get("cpus"),
            "memory_mb": env.get("memory_mb"),
            "storage_mb": env.get("storage_mb"),
            "build_timeout_sec": env.get("build_timeout_sec"),
            "healthcheck_timeout_sec": healthcheck.get("timeout_sec") if healthcheck.get("command") else 0,
            "verifier_timeout_sec": config.get("verifier", {}).get("timeout_sec"),
        })
    return rows


def extremes(rows: list[dict]) -> list[dict]:
    sized = sorted((r for r in rows if r["image_mib"]), key=lambda r: (r["image_mib"], r["task_id"]))
    smallest, largest = dict(sized[0], stratum="image-smallest"), dict(sized[-1], stratum="image-largest")
    return [smallest, largest]


def largest_remainder(counts: Counter, seats: int) -> dict[str, int]:
    total = sum(counts.values())
    quotas = {k: seats * v / total for k, v in counts.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    by_remainder = sorted(quotas, key=lambda k: (-(quotas[k] - alloc[k]), -counts[k], k))
    for k in by_remainder[: seats - sum(alloc.values())]:
        alloc[k] += 1
    return {k: n for k, n in alloc.items() if n}


def pick_code(rows: list[dict]) -> list[dict]:
    chosen = extremes(rows)
    taken = {r["task_id"] for r in chosen}
    seats = largest_remainder(Counter(r["category"] for r in rows), N_CODE - len(chosen))
    for category, n in sorted(seats.items()):
        pool = sorted((r for r in rows if r["category"] == category and r["task_id"] not in taken),
                      key=lambda r: order_key(r["task_id"]))
        for r in pool[:n]:
            chosen.append(dict(r, stratum=f"language:{category}"))
            taken.add(r["task_id"])
    return chosen


def pick_cyber(rows: list[dict]) -> list[dict]:
    chosen = extremes(rows)
    covered = {r["category"] for r in chosen}
    counts = Counter(r["category"] for r in rows)
    projects = [p for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if p not in covered]
    for project in projects[: N_CYBER - len(chosen)]:
        pool = sorted((r for r in rows if r["category"] == project), key=lambda r: order_key(r["task_id"]))
        chosen.append(dict(pool[0], stratum=f"project:{project}"))
    return chosen


def trial_timeout(row: dict, repeat_n: int) -> int:
    """Seconds a nop trial may take: build + healthcheck + every verifier run + margin."""
    return int(row["build_timeout_sec"] + row["healthcheck_timeout_sec"]
               + row["verifier_timeout_sec"] * repeat_n + MARGIN_S)


def spec_source(domain: str, row: dict, task_rel: str, package_digest: str) -> dict:
    repeat_n = REPEAT_N if domain == "terminal" else None
    spec = {
        "task": task_rel,
        "task_package_digest": package_digest,
        "agent": "nop",
        "environment": "daytona",
        "name": f"mimo-qual-{PREFIX[domain]}-{row['task_id'].replace('_', '-')}"[:80],
        "jobs_dir": "runs",
        "attempts": 1,
        "timeout_seconds": trial_timeout(row, repeat_n or 1),
        "purpose": "calibration",
        "hypothesis": ("The MiMo task starts, passes its healthcheck and grades on Daytona: "
                       "a nop control completes the verifier with reward 0."),
        "submitted_by": "har88-qualification",
    }
    if repeat_n:
        spec["verifier_repeat_n"] = repeat_n
    if row["storage_mb"] is None:
        spec["override_storage_mb"] = DAYTONA_MAX_DISK_MB
    return spec


def main() -> None:
    sizes = json.loads((OUT / "image-sizes.json").read_text())["mib"]
    store = task_store_root(ROOT)
    pools = {domain: load_pool(domain, store, sizes) for domain in PINS}
    assert len(pools["terminal"]) == 64 and len(pools["code"]) == 2698, "pool is not the full pinned snapshot"
    selection = {
        "terminal": [dict(r, stratum="all") for r in pools["terminal"]],
        "code": pick_code(pools["code"]),
        "cyber": pick_cyber(pools["cyber"]),
        "music": [dict(r, stratum="local-failure") for r in pools["music"] if r["task_id"] == MUSIC_TASK],
    }
    specs_dir = OUT / "specs"
    for old in specs_dir.glob("mimo-qual-*.json"):
        old.unlink()
    cohort = []
    for domain in ("terminal", "code", "cyber", "music"):
        for row in sorted(selection[domain], key=lambda r: r["task_id"]):
            task_rel = f"{snapshot_rel(domain)}/tasks/{row['task_id']}"
            package = compute_task_digests(row["src"]).package
            spec = spec_source(domain, row, task_rel, package)
            (specs_dir / f"{spec['name']}.json").write_text(json.dumps(spec, indent=2) + "\n")
            cohort.append({
                "task_id": row["task_id"], "domain": domain, "stratum": row["stratum"],
                "category": row["category"], "image_mib": row["image_mib"],
                "cpus": row["cpus"], "memory_mb": row["memory_mb"], "storage_mb": row["storage_mb"],
                "override_storage_mb": spec.get("override_storage_mb"),
                "timeout_seconds": spec["timeout_seconds"],
                "verifier_repeat_n": spec.get("verifier_repeat_n", 1),
                "harbor_digest": harbor_task_digest(row["src"]), "package_digest": package,
                "task": task_rel, "spec": f"specs/{spec['name']}.json",
            })
    counts = Counter(r["domain"] for r in cohort)
    (OUT / "cohort.json").write_text(json.dumps({
        "card": "HAR-88", "agent": "nop", "environment": "daytona", "seed": SEED,
        "pins": {d: f"{o}/{r}@{rev}" for d, (o, r, rev) in PINS.items()},
        "counts": dict(sorted(counts.items())), "cohort": cohort,
    }, indent=2) + "\n")
    print(f"cohort: {len(cohort)} tasks {dict(sorted(counts.items()))}")
    for domain in ("code", "cyber"):
        print(domain, Counter(r["stratum"].split(":")[0] if r["stratum"].startswith("project") else r["stratum"]
                              for r in selection[domain]).most_common())


if __name__ == "__main__":
    main()
