#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("gate", "gate-receipt", "land")
SKIP = "land: gate already passed on tree "
BOUNDARY_STEP = "sync --frozen"
GATE_BOUNDARY_FAKE = f"""#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${{BASH_SOURCE[0]}}")/.."
gate_tree=$(scripts/gate-receipt begin)
uv {BOUNDARY_STEP}
scripts/gate-receipt write "$gate_tree"
"""
FAKE_UV = r"""#!/bin/sh
step="$*"
seen=none
[ -e "$SIM_RECEIPT" ] && seen=receipt
printf '%s|%s\n' "$seen" "$step" >>"$SIM_LOG"
matches() {
  [ -n "$1" ] || return 1
  case "$step" in *"$1"*) return 0 ;; esac
  return 1
}
matches "$SIM_FAIL" && exit 7
matches "$SIM_DIRTY" && printf 'dirty\n' >sim-dirty.txt
matches "$SIM_CLEAN" && rm -f sim-dirty.txt
matches "$SIM_COMMIT" && printf 'more\n' >>notes.txt && git commit -qam during-gate
if matches "$SIM_RACE"; then
  left=$(cat "$SIM_RACE_LEFT")
  if [ "$left" -gt 0 ]; then
    printf '%s\n' "$((left - 1))" >"$SIM_RACE_LEFT"
    printf 'race %s\n' "$left" >>"$SIM_RACE_REPO/race.txt"
    git -C "$SIM_RACE_REPO" commit -qam race || exit 9
    git -C "$SIM_RACE_REPO" push -q origin HEAD:main || exit 9
  fi
fi
exit 0
"""


class Sim:
    def __init__(self, tmp: Path) -> None:
        assert tmp.is_dir()
        self.tmp = tmp
        self.work = tmp / "work"
        self.race = tmp / "race"
        self.log = tmp / "uv.log"
        self.race_left = tmp / "race-left"
        self.lock_dir = tmp / "home" / ".cache" / "evallab-land.lock.d"
        self.steps: list[str] = []
        self.git_dirs: dict[Path, Path] = {}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SIM_", "LAND_"))}
        self.env.update(
            HOME=str(tmp / "home"),
            PATH=f"{tmp / 'bin'}{os.pathsep}{os.environ['PATH']}",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="sim",
            GIT_AUTHOR_EMAIL="sim@example.invalid",
            GIT_COMMITTER_NAME="sim",
            GIT_COMMITTER_EMAIL="sim@example.invalid",
            SIM_LOG=str(self.log),
            SIM_RACE_LEFT=str(self.race_left),
            SIM_RACE_REPO=str(self.race),
        )

    def git(self, cwd: Path, *args: str) -> str:
        proc = subprocess.run(("git", *args), cwd=cwd, env=self.env, capture_output=True, text=True, timeout=600, check=False)
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
        return proc.stdout.strip()

    def script(self, cwd: Path, name: str, *args: str, code: int = 0, **faults: str) -> tuple[str, list[str]]:
        assert name in SCRIPTS
        self.log.write_text("")
        env = dict(self.env, SIM_RECEIPT=str(self.receipt(cwd)), **faults)
        proc = subprocess.run((str(cwd / "scripts" / name), *args), cwd=cwd, env=env, capture_output=True, text=True, timeout=1200, check=False)
        out = proc.stdout + proc.stderr
        assert proc.returncode == code, f"{name} {args} {faults} exited {proc.returncode}, want {code}: {out}"
        return out, self.log.read_text().splitlines()

    def receipt(self, cwd: Path) -> Path:
        if cwd not in self.git_dirs:
            self.git_dirs[cwd] = Path(self.git(cwd, "rev-parse", "--absolute-git-dir"))
        return self.git_dirs[cwd] / "evallab-gate-receipt"

    def tree(self, cwd: Path) -> str:
        return self.git(cwd, "rev-parse", "HEAD^{tree}")

    def head(self, cwd: Path, rev: str = "HEAD") -> str:
        return self.git(cwd, "rev-parse", rev)

    def remote_main(self) -> str:
        return self.git(self.tmp / "remote.git", "rev-parse", "main")

    def commit(self, cwd: Path, name: str, text: str) -> None:
        path = cwd / name
        path.write_text(path.read_text() + text)
        self.git(cwd, "commit", "-q", "-am", f"{name} {text.strip()}")

    def plant(self, cwd: Path) -> None:
        self.script(cwd, "gate-receipt", "write", self.tree(cwd))
        assert self.receipt(cwd).read_bytes() == f"{self.tree(cwd)}\n".encode()

    def land(self, code: int = 0, **faults: str) -> tuple[str, int]:
        out, seen = self.script(self.work, "land", code=code, **faults)
        assert not self.lock_dir.exists()
        return out, sum(1 for line in seen if line.split("|", 1)[1] == BOUNDARY_STEP)

    def install_gate_boundary_fake(self) -> None:
        assert BOUNDARY_STEP in self.steps
        gate = self.work / "scripts" / "gate"
        gate.write_text(GATE_BOUNDARY_FAKE)
        gate.chmod(0o755)
        self.git(self.work, "commit", "-q", "-am", "sim: gate boundary fake for land scenarios")

    def setup(self) -> None:
        (self.tmp / "bin").mkdir()
        (self.tmp / "home").mkdir()
        for name in ("uv", "uvx"):
            (self.tmp / "bin" / name).write_text(FAKE_UV)
            (self.tmp / "bin" / name).chmod(0o755)
        self.race_left.write_text("0\n")
        remote, seed = self.tmp / "remote.git", self.tmp / "seed"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        self.git(self.tmp, "init", "-q", "-b", "main", str(seed))
        (seed / "scripts").mkdir()
        for name in SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, seed / "scripts" / name)
        for name in ("notes.txt", "race.txt", "feature.txt"):
            (seed / name).write_text(f"{name}\n")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-q", "-m", "seed")
        self.git(seed, "push", "-q", str(remote), "main")
        self.git(self.tmp, "clone", "-q", str(remote), "work")
        self.git(self.tmp, "clone", "-q", str(remote), "race")


def gate_certifies_exact_tree(sim: Sim) -> None:
    work = sim.work
    out, seen = sim.script(work, "gate")
    tree = sim.tree(work)
    assert sim.receipt(work).read_bytes() == f"{tree}\n".encode()
    assert f"receipt written for tree {tree}" in out
    assert len(seen) > 2 and all(line.startswith("none|") for line in seen)
    sim.steps = [line.split("|", 1)[1] for line in seen]
    sim.script(work, "gate-receipt", "check")
    print(f"receipt: gate passed {len(seen)} faked steps, receipt = tree {tree} + newline, check exit 0")
    for data in (tree.encode(), f"{tree}\n\n".encode(), f"{tree} \n".encode(), f"{'0' * len(tree)}\n".encode()):
        sim.receipt(work).write_bytes(data)
        sim.script(work, "gate-receipt", "check", code=1)
    sim.receipt(work).write_bytes(f"{tree}\n".encode())
    print("receipt: malformed or mismatched bytes, check exit 1")
    (work / "notes.txt").write_text((work / "notes.txt").read_text() + " \n")
    sim.script(work, "gate-receipt", "check", code=1)
    print("receipt: uncommitted whitespace, check exit 1")
    sim.git(work, "commit", "-q", "-am", "whitespace only")
    assert sim.receipt(work).is_file() and sim.tree(work) != tree
    sim.script(work, "gate-receipt", "check", code=1)
    print(f"receipt: whitespace commit tree {sim.tree(work)}, old receipt kept, check exit 1")


def failed_gate_clears_stale_receipt(sim: Sim) -> None:
    work, steps = sim.work, sim.steps
    for index in (0, len(steps) // 2, len(steps) - 1):
        sim.plant(work)
        out, seen = sim.script(work, "gate", code=7, SIM_FAIL=steps[index])
        first = next(i for i, step in enumerate(steps) if steps[index] in step)
        assert len(seen) == first + 1, out
        assert all(line.startswith("none|") for line in seen)
        assert not sim.receipt(work).exists()
        sim.script(work, "gate-receipt", "check", code=1)
        print(f"failure: step {first + 1}/{len(steps)} '{steps[index]}' exit 7, stale receipt cleared before step 1, check exit 1")


def changed_gate_cannot_certify(sim: Sim) -> None:
    work, mid = sim.work, sim.steps[len(sim.steps) // 2]
    cases = (("dirtied mid-gate", {"SIM_DIRTY": mid}), ("dirty at start", {"SIM_CLEAN": sim.steps[0]}), ("commit mid-gate", {"SIM_COMMIT": mid}))
    for name, faults in cases:
        sim.plant(work)
        if name == "dirty at start":
            (work / "sim-dirty.txt").write_text("start\n")
        out, seen = sim.script(work, "gate", **faults)
        assert len(seen) == len(sim.steps), name
        assert "gate: no receipt" in out and not sim.receipt(work).exists(), name
        (work / "sim-dirty.txt").unlink(missing_ok=True)
        sim.script(work, "gate-receipt", "check", code=1)
        print(f"uncertified: {name}, gate exit 0 without receipt, check exit 1")


def receipts_are_per_worktree(sim: Sim) -> None:
    work, other = sim.work, sim.tmp / "other"
    sim.git(work, "worktree", "add", "-q", "-b", "other", str(other))
    sim.script(work, "gate")
    assert sim.tree(work) == sim.tree(other) and sim.receipt(work) != sim.receipt(other)
    sim.script(other, "gate-receipt", "check", code=1)
    first = sim.receipt(work).read_bytes()
    sim.script(other, "gate")
    assert sim.receipt(work).read_bytes() == first == sim.receipt(other).read_bytes()
    sim.script(work, "gate-receipt", "check")
    sim.script(other, "gate-receipt", "check")
    sim.git(work, "worktree", "remove", str(other))
    print("worktree: same tree, sibling check exit 1 until its own gate; receipts stay separate")


def land_skips_only_exact_tree(sim: Sim) -> None:
    work = sim.work
    sim.install_gate_boundary_fake()
    print(f"land phase: committed synthetic scripts/gate boundary fake (begin, uv {BOUNDARY_STEP}, write); land and helper stay real copies")
    (work / "scratch.txt").write_text("dirty\n")
    out, runs = sim.land(code=1)
    assert "commit or remove worktree changes" in out and runs == 0
    (work / "scratch.txt").unlink()
    print("land dirty: exit 1 before lock, gate not run")
    sim.plant(work)
    out, runs = sim.land(LAND_FORCE_GATE="0")
    tree = sim.tree(work)
    assert f"{SKIP}{tree}; skipping the gate" in out and runs == 0, out
    assert sim.remote_main() == sim.head(work)
    print(f"land skip: '{SKIP}{tree}; skipping the gate', 0 gate runs, pushed")
    sim.commit(work, "feature.txt", "mismatch\n")
    out, runs = sim.land()
    assert SKIP not in out and runs == 1, out
    assert sim.receipt(work).read_bytes() == f"{sim.tree(work)}\n".encode()
    assert sim.remote_main() == sim.head(work)
    print(f"land mismatch: receipt for {tree}, HEAD tree {sim.tree(work)}, 1 gate run, pushed")
    out, runs = sim.land(LAND_FORCE_GATE="1")
    assert SKIP not in out and runs == 1, out
    sim.script(work, "gate-receipt", "check")
    print("land forced: exact receipt present, LAND_FORCE_GATE=1, 1 gate run")


def land_rebases_and_retries_push(sim: Sim) -> None:
    work, race = sim.work, sim.race
    sim.git(race, "pull", "-q", "--rebase", "origin", "main")
    sim.commit(race, "race.txt", "upstream\n")
    sim.git(race, "push", "-q", "origin", "HEAD:main")
    sim.commit(work, "feature.txt", "rebase\n")
    sim.plant(work)
    stale = sim.tree(work)
    out, runs = sim.land()
    assert SKIP not in out and runs == 1 and sim.tree(work) != stale, out
    assert sim.head(work, "HEAD^") == sim.head(race) and sim.remote_main() == sim.head(work)
    print(f"land rebase: pre-rebase receipt {stale} not reused, 1 gate run on rebased tree, pushed")
    sim.git(race, "pull", "-q", "--rebase", "origin", "main")
    sim.commit(work, "feature.txt", "retry\n")
    sim.race_left.write_text("1\n")
    out, runs = sim.land(SIM_RACE=BOUNDARY_STEP)
    assert runs == 2 and "[rejected]" in out and "Landed " in out, out
    assert sim.head(work, "HEAD^") == sim.head(race) and sim.remote_main() == sim.head(work)
    print("land retry: push rejected once, rebased, 2 gate runs, pushed")
    sim.git(race, "pull", "-q", "--rebase", "origin", "main")
    sim.commit(work, "feature.txt", "exhaust\n")
    sim.race_left.write_text("6\n")
    out, runs = sim.land(code=1, SIM_RACE=BOUNDARY_STEP)
    assert runs == 5 and "main changed during all 5 push attempts" in out, out
    assert sim.race_left.read_text().strip() == "1"
    assert sim.remote_main() == sim.head(race) != sim.head(work)
    print("land exhaust: 5 rejected push attempts, 5 gate runs, exit 1, remote unchanged by land")


SCENARIOS = (
    gate_certifies_exact_tree,
    failed_gate_clears_stale_receipt,
    changed_gate_cannot_certify,
    receipts_are_per_worktree,
    land_skips_only_exact_tree,
    land_rebases_and_retries_push,
)


def main() -> None:
    assert shutil.which("git"), "git is required"
    with tempfile.TemporaryDirectory(prefix="gate-receipt-sim-", dir="/tmp") as name:
        sim = Sim(Path(name).resolve())
        sim.setup()
        for scenario in SCENARIOS:
            scenario(sim)
    print(f"gate-receipt sim: {len(SCENARIOS)} scenarios passed")


if __name__ == "__main__":
    main()
