#!/usr/bin/env python3
"""Make generated DB-reward tasks oracle-runnable without a model credential.

The upstream template uses strict `${OPENAI_API_KEY}` interpolation even for
DB-only oracle grading. Replacing it with an empty default preserves the
variable when present (reference runs) while allowing the deterministic oracle
and verifier to start without a model key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPLACEMENTS = {
    "${OPENAI_API_KEY}": "${OPENAI_API_KEY:-}",
    "${OPENAI_BASE_URL}": "${OPENAI_BASE_URL:-}",
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("generated_root", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    files = sorted(args.generated_root.glob("tau3-banking_knowledge-task-*/task.toml"))
    if len(files) != 8:
        raise SystemExit(f"expected 8 bounded task manifests, found {len(files)}")
    records = []
    for path in files:
        before = sha(path)
        content = path.read_text(encoding="utf-8")
        if all(old not in content for old in REPLACEMENTS):
            raise RuntimeError(f"task manifest has no strict credential placeholders: {path}")
        for old, new in REPLACEMENTS.items():
            content = content.replace(old, new)
        path.write_text(content, encoding="utf-8")
        records.append({"path": str(path), "before_sha256": f"sha256:{before}", "after_sha256": f"sha256:{sha(path)}", "oracle_credential_required": False})
    evidence = args.evidence or args.generated_root.parent / "oracle-config-evidence.json"
    evidence.write_text(json.dumps({"schema_version":"tau-oracle-config/v1", "files":records}, indent=2) + "\n", encoding="utf-8")
    print(f"prepared {len(records)} oracle task manifests with optional credentials")
    return 0


if __name__ == "__main__":
    main()
