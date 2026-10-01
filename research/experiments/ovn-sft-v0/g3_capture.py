"""Captured-call plumbing shared by ``qualify_reconstruction.py`` and ``freeze_sft.py``.

* :func:`split_capture` attributes a capture's calls to the jobs' trials with
  ``evallab.model_capture.link_capture`` and writes one ``<trial>.jsonl`` per
  trial (only that trial's calls, in ``seq`` order), with per-trial link health;
* :func:`check_fidelity` runs ``fidelity.py`` with those files bound per trial;
* :func:`witness` checks the one-to-one row/call coverage of one trial;
* :func:`producer` digests what produces and checks the export (exporter,
  checker, capture linker, tokenizer files, library versions), so a frozen
  set can prove it used the qualified reconstructor.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
UV_TOKENIZER = ["uv", "run", "--no-project", "--with", "transformers", "--with", "jinja2"]
#: What ``AutoTokenizer`` and the chat template read; never weight shards.
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
)
PRODUCER_FILES = (
    "src/evallab/sft_terminus.py",
    "src/evallab/model_capture.py",
    "research/experiments/ovn-sft-v0/fidelity.py",
    "research/experiments/ovn-sft-v0/g3_capture.py",
)


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def delivered(call: dict[str, Any]) -> bool:
    """Same rule as ``fidelity.delivered``: 200, no ``error``, not truncated."""
    body = call.get("response_body")
    return (
        call.get("error") is None
        and call.get("response_status") == 200
        and not (isinstance(body, dict) and body.get("_truncated"))
    )


def split_capture(capture_dir: Path, jobs: list[Path], out: Path) -> dict[str, Any]:
    """Link ``capture_dir/calls.jsonl`` to every job's trials; write per-trial files.

    Returns ``{"calls": {seq: call}, "unassigned": [seq], "links": [...],
    "trials": {trial: {"file", "seqs", "delivered", "attribution", "ambiguous"}}}``.
    A call no job claims is ``unassigned``: run this over every job that used
    the capture, so an unassigned call means a call nobody can account for.
    """
    import pyarrow.parquet as pq

    from evallab.model_capture import link_capture

    calls_path = capture_dir / "calls.jsonl"
    calls = {
        int(call["seq"]): call
        for call in (json.loads(line) for line in calls_path.read_text().splitlines() if line)
    }
    seqs: dict[str, list[int]] = defaultdict(list)
    attribution: dict[str, Counter] = defaultdict(Counter)
    ambiguous: set[str] = set()
    links = []
    for job in jobs:
        receipt = link_capture(capture_dir, job, derived_root=out / "link")
        table = pq.read_table(Path(receipt["parquet_dir"]) / "model_calls.parquet").to_pylist()
        for row in table:
            if row["trial_name"]:
                seqs[row["trial_name"]].append(int(row["seq"]))
                attribution[row["trial_name"]][row["attribution"]] += 1
        ambiguous.update(receipt["ambiguous_trials"])
        links.append(
            {
                "job": job.name,
                "calls_assigned": receipt["calls_assigned"],
                "ambiguous_trials": receipt["ambiguous_trials"],
                "receipt": sha256(Path(receipt["parquet_dir"]) / "capture_link.json"),
            }
        )
    (out / "captures").mkdir(parents=True, exist_ok=True)
    trials = {}
    for trial, trial_seqs in sorted(seqs.items()):
        ordered = sorted(trial_seqs)
        path = out / "captures" / f"{trial}.jsonl"
        path.write_text("".join(json.dumps(calls[s], ensure_ascii=False) + "\n" for s in ordered))
        trials[trial] = {
            "file": path,
            "seqs": ordered,
            "delivered": [s for s in ordered if delivered(calls[s])],
            "attribution": dict(attribution[trial]),
            "ambiguous": trial in ambiguous,
        }
    claimed = {s for t in trials.values() for s in t["seqs"]}
    return {
        "calls_sha256": sha256(calls_path),
        "calls": calls,
        "unassigned": sorted(set(calls) - claimed),
        "links": links,
        "trials": trials,
    }


def check_fidelity(
    export: Path,
    tokenizer: Path,
    capture: dict[str, Any] | None,
    *,
    require_capture: bool = False,
    show: int = 0,
) -> dict[str, Any]:
    """Run ``fidelity.py`` on an export; returns its ``fidelity.json``."""
    command = [*UV_TOKENIZER, "python", str(HERE / "fidelity.py"), str(export)]
    command += ["--tokenizer", str(tokenizer), "--gate-target", "--show", str(show)]
    if require_capture:
        command.append("--require-capture")
    for trial, entry in (capture or {}).get("trials", {}).items():
        command += ["--capture", f"{trial}={entry['file']}"]
    subprocess.run(command, check=False, capture_output=True)
    return json.loads((export / "fidelity.json").read_text())


def witness(rows: list[dict[str, Any]], entry: dict[str, Any] | None) -> dict[str, Any]:
    """One trial's row/call coverage: each row identical to its own delivered
    call, no call backing two rows, and which delivered calls no row stands at.
    ``rows`` are that trial's ``fidelity.json`` row results."""
    matched = [r.get("capture_seq") for r in rows if r.get("capture") == "identical"]
    delivered_seqs = set(entry["delivered"]) if entry else set()
    return {
        "rows": len(rows),
        "rows_identical": len(matched),
        "one_call_per_row": len(matched) == len(set(matched)) and None not in matched,
        "delivered_calls": len(delivered_seqs),
        "delivered_without_row": sorted(delivered_seqs - set(matched)),
        "undelivered_calls": sorted(set(entry["seqs"]) - delivered_seqs) if entry else [],
        "link_ambiguous": bool(entry and entry["ambiguous"]),
        "attribution": entry["attribution"] if entry else {},
    }


def producer(tokenizer: Path) -> dict[str, Any]:
    """Digests of everything that produces or checks an export."""
    versions = subprocess.run(
        [
            *UV_TOKENIZER,
            "python",
            "-c",
            "import json, jinja2, transformers, tokenizers;"
            "print(json.dumps({'transformers': transformers.__version__,"
            " 'tokenizers': tokenizers.__version__, 'jinja2': jinja2.__version__}))",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()[-1]
    return {
        "files": {name: sha256(ROOT / name) for name in PRODUCER_FILES},
        "tokenizer_files": {
            name: sha256(tokenizer / name)
            for name in TOKENIZER_FILES
            if (tokenizer / name).is_file()
        },
        "libraries": json.loads(versions),
    }
