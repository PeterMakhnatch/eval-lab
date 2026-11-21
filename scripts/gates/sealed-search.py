from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
SEALED_PATHS = (
    "research/answer-keys/*",
    "research/datasets/*/test.jsonl",
    "research/datasets/qa-trace-sft-v0/baseline-results.json",
    "research/datasets/qa-trace-sft-v0/harness-v2-results.json",
    "research/datasets/qa-trace-sft-v0/harness-v3-results.json",
    "research/datasets/seeker-sft-v1/quarantined.jsonl",
    "research/datasets/seeker-sft-v1/source_manifests.json",
)


def search_ignored_paths(root: Path, paths: Sequence[str]) -> set[str]:
    ignore = root / ".ignore"
    assert ignore.is_file(), f"missing {ignore}"
    with TemporaryDirectory(prefix="evallab-ignore-") as directory:
        scratch = Path(directory)
        git_dir = scratch / ".git"
        (git_dir / "objects").mkdir(parents=True)
        (git_dir / "refs").mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        result = run(
            [
                "git", f"--git-dir={git_dir}", f"--work-tree={scratch}",
                "-c", f"core.excludesFile={ignore}",
                "check-ignore", "--no-index", "-z", "--stdin",
            ],
            cwd=scratch,
            input=b"".join(path.encode() + b"\0" for path in paths),
            capture_output=True,
        )
        if result.returncode not in {0, 1}:
            result.check_returncode()
    return set(result.stdout.decode().split("\0")) - {""}


def main() -> int:
    result = run(["git", "ls-files", "-z", "--", "research"], cwd=ROOT, check=True, capture_output=True)
    sealed = sorted(
        path for path in result.stdout.decode().split("\0")
        if any(fnmatchcase(path, pattern) for pattern in SEALED_PATHS)
    )
    if len(search_ignored_paths(ROOT, sealed)) != len(sealed):
        print("Sealed paths are not all covered by the repository .ignore file.")
        return 1
    print("Sealed search exclusions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
