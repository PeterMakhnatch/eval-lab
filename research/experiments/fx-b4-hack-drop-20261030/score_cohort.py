"""Locate and score the B4b cohort. Read-only journal. No model calls.

Writes a receipt JSON. Does not change the frozen rules in ``hack_drop.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from hack_drop import score_path

from evallab.production.seeker_worker import compute_candidate_digest

JOURNAL = Path("/Users/petermakhnatch/.local/state/evallab-prod-live/prod.sqlite")
CAS = Path("/Users/petermakhnatch/.local/state/evallab-prod-live/cas/sha256")
EVIDENCE_SEEKER = Path("/Users/petermakhnatch/.local/state/evallab-prod-live/evidence/seeker")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _id_order(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_events(event_type: str) -> list[dict[str, Any]]:
    uri = f"file:{JOURNAL}?mode=ro&immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT seq, event_id, envelope FROM events WHERE event_type = ? ORDER BY seq",
            (event_type,),
        ).fetchall()
    finally:
        con.close()
    loaded = []
    for seq, event_id, envelope in rows:
        event = json.loads(envelope)
        loaded.append(
            {
                "seq": seq,
                "event_id": event_id,
                "run_id": event.get("run_id"),
                "payload": event["payload"],
            }
        )
    return loaded


def _bundle_digest(path: Path) -> str | None:
    if not path.is_dir():
        return None
    files: dict[str, str] = {}
    for child in path.iterdir():
        if child.is_file():
            files[child.name] = child.read_text(encoding="utf-8", errors="replace")
    if not files:
        return None
    return compute_candidate_digest(files)


def _looked(label: str, path: str, found: bool, note: str = "") -> dict[str, Any]:
    return {"place": label, "path": path, "found": found, "note": note}


def locate_exploits() -> list[dict[str, Any]]:
    rows = []
    for event in _load_events("seeker.exploit_confirmed"):
        payload = event["payload"]
        bundle = Path(str(payload.get("solution_bundle") or ""))
        digest = str(payload.get("candidate_digest") or "")
        hexdigest = digest.removeprefix("sha256:")
        cas_path = CAS / hexdigest
        exploit_run = Path(str(payload.get("exploit_run") or ""))
        computed = _bundle_digest(bundle) if bundle.is_dir() else None
        solve = bundle / "solve.sh"
        places = [
            _looked("solution_bundle", str(bundle), bundle.is_dir()),
            _looked(
                "candidate_digest_cas",
                str(cas_path),
                cas_path.is_file(),
                "journal digest is the bundle digest, not a CAS blob",
            ),
            _looked("exploit_run", str(exploit_run), exploit_run.exists()),
            _looked(
                "evidence_seeker_root",
                str(EVIDENCE_SEEKER),
                EVIDENCE_SEEKER.is_dir(),
                "copy search is not required once the bundle digest matches",
            ),
        ]
        pair = f"{payload.get('dataset_id')}:{payload.get('task_id')}"
        rows.append(
            {
                "kind": "exploit",
                "seq": event["seq"],
                "event_id": event["event_id"],
                "run_id": event["run_id"],
                "pair": pair,
                "task_id": payload.get("task_id"),
                "dataset_id": payload.get("dataset_id"),
                "attempt_index": payload.get("attempt_index"),
                "id": f"{pair}:attempt-{payload.get('attempt_index')}:seq-{event['seq']}",
                "candidate_digest": digest,
                "digest_match": computed == digest,
                "computed_digest": computed,
                "scored_path": str(bundle) if bundle.is_dir() and computed == digest else None,
                "solve_sha256": _sha256_file(solve) if solve.is_file() else None,
                "solve_bytes": solve.stat().st_size if solve.is_file() else None,
                "places": places,
            }
        )
    return rows


def locate_references() -> list[dict[str, Any]]:
    rows = []
    for event in _load_events("task_quality.repair_qualified"):
        payload = event["payload"]
        refs = (payload.get("control_evidence") or {}).get("reference") or []
        oracle = {}
        if refs:
            oracle = ((refs[0].get("execution") or {}).get("facts") or {}).get("oracle") or {}
        oracle_sha = str(oracle.get("sha256") or "")
        patch_path = str(payload.get("reference_patch") or "")
        cas_path = CAS / oracle_sha if oracle_sha else None
        cas_ok = bool(cas_path and cas_path.is_file() and _sha256_file(cas_path) == oracle_sha)
        bundle = Path(str(payload.get("solution_bundle") or ""))
        places = [
            _looked("reference_patch", patch_path, Path(patch_path).is_file(), "journal path"),
            _looked(
                "oracle_sha256_cas",
                str(cas_path) if cas_path else "",
                cas_ok,
                "sha256 of the file must equal the oracle digest",
            ),
            _looked("solution_bundle", str(bundle), bundle.is_dir()),
            _looked(
                "repaired_task",
                str(payload.get("repaired_task") or ""),
                Path(str(payload.get("repaired_task") or "")).exists(),
                "task package, not the reference solution",
            ),
            _looked(
                "original_task_package",
                str(payload.get("original_task_package") or ""),
                Path(str(payload.get("original_task_package") or "")).exists(),
                "task package, not the reference solution",
            ),
        ]
        task_id = str(payload.get("task_id"))
        rows.append(
            {
                "kind": "reference",
                "seq": event["seq"],
                "event_id": event["event_id"],
                "run_id": event["run_id"],
                "task_id": task_id,
                "dataset_id": payload.get("dataset_id"),
                "id": f"mimo-defect-map-v1:{task_id}:seq-{event['seq']}",
                "oracle_sha256": oracle_sha,
                "patch_path": str(cas_path) if cas_ok else None,
                "patch_bytes": cas_path.stat().st_size if cas_ok and cas_path else None,
                "bundle_path": str(bundle) if bundle.is_dir() else None,
                "places": places,
            }
        )
    return rows


def _score_row(row: dict[str, Any], path_key: str) -> dict[str, Any]:
    path = row.get(path_key)
    if not path:
        return {"scored": False, "reason": "bytes not located"}
    report = score_path(path)
    report["scored"] = True
    report["path"] = path
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    exploits = locate_exploits()
    references = locate_references()
    for row in exploits:
        row["score"] = _score_row(row, "scored_path")
    for row in references:
        row["patch_score"] = _score_row(row, "patch_path")
        row["bundle_score"] = _score_row(row, "bundle_path")
    exploits.sort(key=lambda row: _id_order(row["id"]))
    references.sort(key=lambda row: _id_order(row["id"]))
    payload = {"exploits": exploits, "references": references}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} exploits={len(exploits)} references={len(references)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
