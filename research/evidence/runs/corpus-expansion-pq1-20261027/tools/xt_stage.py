#!/usr/bin/env python3
"""Stage an external labeled-cheat corpus as a local, cross-test-only bundle (pQ-SELF-1).

Standard library only. One tool for every Hugging Face dataset we cross-test on (HackBench was staged with
its own tool in LB-6; this generalises it):

* ``plan``   list the files of a pinned revision with sizes and the digests the Hub reports, the licence
             tag and the gate state, and say whether the licence passes the clean-licence check;
* ``fetch``  download a pinned revision into ``~/.local/state/evallab-cross-test/<slug>/<rev12>/`` (outside
             every git checkout, the production database and the content store), verify every file against
             the Hub's SHA-256 (LFS files) or Git blob SHA-1, and write ``STAGE-MANIFEST.json`` and
             ``CROSS_TEST_ONLY.md``;
* ``verify`` re-check a staged bundle against its manifest;
* ``lock``   make the bundle read-only.

The token (``HF_TOKEN``) is read from the environment (run as ``keys run -- python xt_stage.py fetch ...``)
and is never printed or stored. The tool refuses to fetch a dataset whose licence tag is missing, ``other`` or
not on the clean list, unless ``--licence-ok REASON`` records why that is acceptable. It never accepts a gate
for you: a gated dataset that answers 401/403 stops with "needs the gate accepted by the account owner".

Cross-test rules the bundle's notice repeats: freeze the detector design first, run once, never tune on it,
never train or retrieve examples from it, never copy it to the production database, the content store, Modal,
R2 or a git repository; receipts hold counts, indices and digests only.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

HUB = "https://huggingface.co"
STATE = Path.home() / ".local/state/evallab-cross-test"
CLEAN_LICENCES = frozenset(
    {
        "mit",
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "cc0-1.0",
        "cc-by-3.0",
        "cc-by-4.0",
        "cc-by-sa-3.0",
        "cc-by-sa-4.0",
        "cc-by-nc-4.0",
        "cc-by-nc-sa-4.0",
        "odc-by",
    }
)
MANIFEST = "STAGE-MANIFEST.json"
NOTICE = "CROSS_TEST_ONLY.md"


def _request(url: str, token: str | None) -> urllib.request.Request:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return urllib.request.Request(url, headers=headers)


def api(path: str, token: str | None) -> Any:
    last: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(_request(f"{HUB}{path}", token), timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            if error.code in (401, 403, 404):
                raise
            last = error
        except urllib.error.URLError as error:
            last = error
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Hub API failed: {path}: {last}")


def slug(repo: str) -> str:
    return repo.replace("/", "__").lower()


def default_root(repo: str, revision: str) -> Path:
    return STATE / slug(repo) / revision[:12]


def licence_of(info: dict[str, Any]) -> str | None:
    for tag in info.get("tags", []):
        if tag.startswith("license:"):
            return tag.split(":", 1)[1]
    card = info.get("cardData") or {}
    value = card.get("license")
    return value if isinstance(value, str) else None


def plan(
    repo: str, revision: str | None, token: str | None, include: list[str], exclude: list[str]
) -> dict[str, Any]:
    base = f"/api/datasets/{repo}" + (f"/revision/{revision}" if revision else "")
    info = api(f"{base}?blobs=true", token)
    files = []
    for item in info.get("siblings", []):
        name = item["rfilename"]
        if include and not any(fnmatch.fnmatch(name, pattern) for pattern in include):
            continue
        if any(fnmatch.fnmatch(name, pattern) for pattern in exclude):
            continue
        lfs = item.get("lfs") or {}
        files.append(
            {
                "path": name,
                "bytes": lfs.get("size", item.get("size")),
                "sha256": lfs.get("sha256"),
                "blob_id": item.get("blobId"),
            }
        )
    licence = licence_of(info)
    card = info.get("cardData") or {}
    return {
        "repo": repo,
        "revision": info["sha"],
        "licence": licence,
        "licence_clean": licence in CLEAN_LICENCES,
        "gated": info.get("gated"),
        "gate_terms": {
            k: card.get(k) for k in ("extra_gated_prompt", "extra_gated_fields") if card.get(k)
        },
        "last_modified": info.get("lastModified"),
        "files": files,
        "total_bytes": sum(f["bytes"] or 0 for f in files),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def write_notice(root: Path, p: dict[str, Any], licence_reason: str | None) -> None:
    terms = p["gate_terms"]
    lines = [
        f"# {p['repo']} @ {p['revision']} - CROSS-TEST ONLY, LOCAL ONLY",
        "",
        f"Staged {time.strftime('%Y-%m-%d')} by the Research tab 'library' (wK:pQ). Licence: `{p['licence']}`"
        + (f" (accepted because: {licence_reason})" if licence_reason else "")
        + f". Gate: `{p['gated']}`.",
        "",
        "Use: offline evaluation of detectors on a frozen design, once. Freeze rules and prompts (hash them in a",
        "freeze file) before the first run. A change made after a result has been seen turns this set into tuning",
        "data for that change; report it as such, never as a cross-test. Do not train on it, do not draw few-shot",
        "or retrieval examples from it, do not distil playbook or Hackbook entries from it.",
        "Do not copy it to the production database, the content store, Modal, R2, a git repository, a shared",
        "drive or a public link. Receipts hold counts, indices and digests only: no text from the corpus.",
        "Check the licence terms of the dataset card before any use beyond local evaluation.",
    ]
    if terms:
        lines += [
            "",
            "Gate terms from the dataset card:",
            "",
            "```json",
            json.dumps(terms, indent=1),
            "```",
        ]
    (root / NOTICE).write_text("\n".join(lines) + "\n")


def fetch(
    repo: str,
    revision: str | None,
    include: list[str],
    exclude: list[str],
    root: Path | None,
    licence_ok: str | None,
    max_bytes: int,
) -> Path:
    token = os.environ.get("HF_TOKEN")
    p = plan(repo, revision, token, include, exclude)
    if not p["licence_clean"] and not licence_ok:
        sys.exit(
            f"refusing: licence {p['licence']!r} is not on the clean list; pass --licence-ok REASON if it is acceptable"
        )
    if p["total_bytes"] > max_bytes:
        sys.exit(f"refusing: {p['total_bytes']} bytes exceeds --max-bytes {max_bytes}")
    target = root or default_root(repo, p["revision"])
    target.mkdir(parents=True, exist_ok=True)
    STATE.mkdir(parents=True, exist_ok=True)
    STATE.chmod(0o700)
    for item in p["files"]:
        dest = target / item["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.chmod(0o600)
        url = f"{HUB}/datasets/{repo}/resolve/{p['revision']}/{urllib.parse.quote(item['path'])}"
        try:
            with (
                urllib.request.urlopen(_request(url, token), timeout=300) as response,
                dest.open("wb") as out,
            ):
                while block := response.read(1 << 20):
                    out.write(block)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                sys.exit(
                    f"HTTP {error.code} on {item['path']}: needs the gate accepted by the account owner (not done here)"
                )
            raise
        if item["sha256"]:
            got = sha256_file(dest)
            ok = got == item["sha256"]
        else:
            got = git_blob_sha1(dest)
            ok = got == item["blob_id"]
        if not ok:
            sys.exit(f"digest mismatch on {item['path']}: got {got}")
        item["sha256_computed"] = sha256_file(dest) if not item["sha256"] else item["sha256"]
        item["verified_against"] = "hub_lfs_sha256" if item["sha256"] else "hub_git_blob_sha1"
        print(f"ok {item['path']} {dest.stat().st_size}")
    p["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p["licence_override_reason"] = licence_ok
    p["tool_sha256"] = sha256_file(Path(__file__))
    (target / MANIFEST).write_text(json.dumps(p, indent=1) + "\n")
    write_notice(target, p, licence_ok)
    return target


def verify(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / MANIFEST).read_text())
    problems = []
    for item in manifest["files"]:
        path = root / item["path"]
        if not path.exists():
            problems.append(f"missing {item['path']}")
        elif path.stat().st_size != item["bytes"]:
            problems.append(f"size {item['path']}")
        elif item["sha256"] and sha256_file(path) != item["sha256"]:
            problems.append(f"sha256 {item['path']}")
        elif not item["sha256"] and git_blob_sha1(path) != item["blob_id"]:
            problems.append(f"blob {item['path']}")
    return {
        "repo": manifest["repo"],
        "revision": manifest["revision"],
        "files": len(manifest["files"]),
        "total_bytes": manifest["total_bytes"],
        "problems": problems,
    }


def lock(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "fetch"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True)
        s.add_argument("--revision")
        s.add_argument("--include", action="append", default=[])
        s.add_argument("--exclude", action="append", default=[])
        if name == "fetch":
            s.add_argument("--root", type=Path)
            s.add_argument(
                "--licence-ok", help="reason, when the licence tag is not on the clean list"
            )
            s.add_argument("--max-bytes", type=int, default=20 * 1024**3)
    for name in ("verify", "lock"):
        s = sub.add_parser(name)
        s.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    if args.cmd == "plan":
        print(
            json.dumps(plan(args.repo, args.revision, token, args.include, args.exclude), indent=1)
        )
    elif args.cmd == "fetch":
        print(
            fetch(
                args.repo,
                args.revision,
                args.include,
                args.exclude,
                args.root,
                args.licence_ok,
                args.max_bytes,
            )
        )
    elif args.cmd == "verify":
        result = verify(args.root)
        print(json.dumps(result, indent=1))
        sys.exit(1 if result["problems"] else 0)
    else:
        lock(args.root)
        print("locked read-only", args.root)


if __name__ == "__main__":
    main()
