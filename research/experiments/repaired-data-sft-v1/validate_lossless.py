"""Independently check that `to_export.py`'s `all/` export is a lossless copy of QA Trace's arm A.

For every source row (build_arms `A.jsonl`), matched to its export line through
`provenance.jsonl`, the script checks four things:
1. **Load.** `sft.verify_export` (the Modal trainer's own loader) loads the export, and
   rows out == rows in.
2. **Render equality.** The chat-template render of the exported messages equals the
   render of the recorded messages and tools, byte for byte. This covers reasoning, tool
   calls, tool results and the tools schema.
3. **Fields.** Every non-message field of the source row is present and equal in
   provenance.
4. **Hashes.** The recorded messages and tools sha256 in provenance match the source.

It writes a JSON receipt and exits non-zero on any failure.

    HF_HUB_OFFLINE=1 uv run --project tools/modal-mimo-sft --locked python \
        research/experiments/repaired-data-sft-v1/validate_lossless.py \
        --source <build_arms out>/A.jsonl --export <exports>/all --out receipt.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools/modal-mimo-sft"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sft  # noqa: E402
from to_export import structured  # noqa: E402


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--export", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    tok = sft.get_tokenizer()
    source_bytes = args.source.read_bytes()
    source = {
        r["execution_id"]: r
        for r in (json.loads(line) for line in source_bytes.decode().splitlines() if line.strip())
    }
    # Raw hashes come from the parsed source lines before any helper touches them.
    raw_hashes = {
        key: (
            sha(json.dumps(row["messages"], sort_keys=True).encode()),
            sha(json.dumps(row.get("tools"), sort_keys=True).encode()),
        )
        for key, row in source.items()
    }
    export = sft.verify_export(args.export)
    provenance = [
        json.loads(line)
        for line in (args.export / "provenance.jsonl").read_text().splitlines()
        if line.strip()
    ]
    failures: list[str] = []
    if len(provenance) != len(export.conversations):
        failures.append(
            f"provenance {len(provenance)} lines != conversations {len(export.conversations)}"
        )
    seen: set[str] = set()
    fields_checked = 0
    for prov, conv in zip(provenance, export.conversations, strict=False):
        key = prov["execution_id"]
        seen.add(key)
        row = source.get(key)
        if row is None:
            failures.append(f"{key}: not in source")
            continue
        want = tok.apply_chat_template(
            structured(row["messages"]),
            tools=row.get("tools"),
            tokenize=False,
            enable_thinking=True,
        )
        got = tok.apply_chat_template(
            sft.to_template_messages(conv.messages), tokenize=False, enable_thinking=True
        )
        if want != got:
            failures.append(f"{key}: render differs")
        for field, value in row.items():
            if field in ("messages", "tools"):
                continue
            fields_checked += 1
            if prov.get(field) != value:
                failures.append(f"{key}: field {field} differs")
        if prov["source_messages_sha256"] != raw_hashes[key][0]:
            failures.append(f"{key}: messages sha256 differs")
        if prov["source_tools_sha256"] != raw_hashes[key][1]:
            failures.append(f"{key}: tools sha256 differs")
    unchanged = [
        key
        for key, row in source.items()
        if sha(json.dumps(row["messages"], sort_keys=True).encode()) == raw_hashes[key][0]
    ]
    if len(unchanged) != len(source):
        failures.append(f"{len(source) - len(unchanged)} source rows were mutated during the check")
    missing = sorted(set(source) - seen)
    if missing:
        failures.append(f"{len(missing)} source rows missing from export: {missing[:3]}")
    receipt = {
        "source": str(args.source),
        "source_sha256": sha(source_bytes),
        "source_sha256_after": sha(args.source.read_bytes()),
        "source_messages_hash_match": sum(
            1
            for p in provenance
            if raw_hashes.get(p["execution_id"], (None,))[0] == p["source_messages_sha256"]
        ),
        "export": str(args.export),
        "export_conversations_sha256": export.conversations_sha256,
        "rows_in": len(source),
        "rows_out": len(export.conversations),
        "render_equal": len(export.conversations) - sum("render differs" in f for f in failures),
        "provenance_fields_checked": fields_checked,
        "agents": {
            agent: sum(1 for r in source.values() if r["agent"] == agent)
            for agent in sorted({r["agent"] for r in source.values()})
        },
        "failures": failures,
        "ok": not failures,
    }
    args.out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
