"""Download the three pinned rollout corpora and verify their sha256 digests.

Nothing here is committed to git: the files (~450 MB) land under
``--data-root``. Re-running skips files whose digest already matches.
``HF_TOKEN`` is sent when set, though the pinned revisions download without
one. Usage::

    uv run python research/experiments/live-stop-thresholds/fetch.py \
        --data-root /private/tmp/live-stop-data
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from corpora import AISI_FILE, ARIA_FILE, TA_DIR  # noqa: E402

_HF = "https://huggingface.co/datasets"
PINS = (
    (
        ARIA_FILE,
        f"{_HF}/gutenbergpbc/aria-reward-hacking/resolve/"
        "a564b502e190158a14bbec2c7a43bd498daf320f/data/aria_reward_hacking_v0_6.parquet",
        "59ffb1aa73d341f6bd070f639028da8ee041176c8c8e0e18678a4d6751a11556",
    ),
    (
        AISI_FILE,
        f"{_HF}/ai-safety-institute/reward-hacking-olmo3.1-32b-kl0.0-seed2-rollouts/resolve/"
        "adf70b44d4243bcc37e3a90cef88977103db3000/data/train-00000-of-00001.parquet",
        "00bd6ba463f8d53ef5ce8bcecf32803e09f201b8778443ae5e8f63324f45fade",
    ),
    (
        "ta.zip",
        "https://media.githubusercontent.com/media/LichenLillc/CoTMonitoring/"
        "bea6b67e9707b1f2c2ce2fa9304f5834855d6a20/datasets/"
        "trace_and_amplify_reward_hacking_data_v1.zip",
        "e662cd5eb31c5adfc95dc3841d12f567b3edc7db00bd185ff69f4322598a180f",
    ),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(relative: str, url: str, expected: str, data_root: Path) -> Path:
    """Download ``url`` to ``data_root/relative`` unless a verified copy exists."""
    target = data_root / relative
    if target.exists() and sha256(target) == expected:
        print(f"ok (cached) {relative}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url)
    token = os.environ.get("HF_TOKEN")
    if token and url.startswith(_HF):
        request.add_header("Authorization", f"Bearer {token}")
    partial = target.with_name(target.name + ".partial")
    with urllib.request.urlopen(request) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1 << 20)
    actual = sha256(partial)
    if actual != expected:
        partial.unlink()
        raise SystemExit(f"sha256 mismatch for {relative}: {actual} != {expected}")
    partial.replace(target)
    print(f"ok {relative}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args(argv)
    for relative, url, expected in PINS:
        fetch(relative, url, expected, args.data_root)
    ta_root = args.data_root / TA_DIR
    if not ta_root.exists():
        with zipfile.ZipFile(args.data_root / "ta.zip") as archive:
            archive.extractall(args.data_root / "ta")
        print(f"unzipped {TA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
