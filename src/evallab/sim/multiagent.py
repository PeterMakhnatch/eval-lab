from __future__ import annotations

from pathlib import Path
from random import Random

from evallab.sim._multiagent_bridge import check_unified_bridge_invariants
from evallab.sim.engine import Simulation
from evallab.sim.multiagent_gen import (
    build_olam_crucible_trial,
    build_olam_muster_trial,
)
from evallab.sim.multiagent_gen_bespoke import (
    build_bespoke_public_md,
    build_bespoke_trial,
    build_decompose_arms,
    build_task_dir,
)
from evallab.sim.multiagent_hardening import check_hardening_invariants
from evallab.sim.multiagent_invariants import (
    check_bespoke_public_invariants,
    check_decompose_invariants,
    check_executive_dashboard_invariants,
    check_seat_cli_parser_invariants,
    check_task_lint_invariants,
    check_trial_invariants,
)

_CANARY_SECRET = "CANARY_OUTSIDE_TRIAL_SECRET_9f8e7d6c"


class MultiAgentSimulation:
    def __init__(self, sim: Simulation, root: Path) -> None:
        self.sim = sim
        self.root = root
        self.rng = Random(sim.seed ^ 0x51A7F00D)

    def run(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        canary = self.root / "CANARY_SECRET.json"
        canary.write_text(f'{{"secret": "{_CANARY_SECRET}"}}\n', encoding="utf-8")
        scratch = self.root / "out"
        scratch.mkdir(exist_ok=True)
        t0 = float(self.sim.runtime.seconds)
        heavy = (self.sim.seed % 8) == 0

        choice = self.sim.seed % 3
        if choice == 0:
            tdir = build_olam_muster_trial(self.root / "trials", self.rng, t0, canary)
        elif choice == 1:
            tdir = build_olam_crucible_trial(self.root / "trials", self.rng, t0, canary)
        else:
            tdir = build_bespoke_trial(
                self.root / "trials", self.rng, t0, canary, with_git_bundle=heavy
            )

        check_trial_invariants(self.sim, tdir, scratch, _CANARY_SECRET, export_parquet=heavy)

        if self.sim.seed % 4 == 0:
            solo, silent, team = build_decompose_arms(self.root / "arms", self.rng, t0)
            check_decompose_invariants(self.sim, solo, silent, team)
        if self.sim.seed % 4 == 1:
            tasks_root = self.root / "tasks"
            task_dir, expected_errors = build_task_dir(tasks_root, self.rng, canary)
            check_task_lint_invariants(self.sim, task_dir, expected_errors, _CANARY_SECRET)
            build_task_dir(tasks_root, self.rng, canary)
            check_task_lint_invariants(self.sim, tasks_root, None, _CANARY_SECRET)
        if self.sim.seed % 4 == 2:
            md_path = build_bespoke_public_md(self.root / "public", self.rng, t0)
            check_bespoke_public_invariants(self.sim, md_path)
            check_seat_cli_parser_invariants(self.sim, self.root, tdir)
            check_hardening_invariants(self.sim, self.rng)
        if self.sim.seed % 4 == 3:
            solo, silent, team = build_decompose_arms(self.root / "exec_arms", self.rng, t0)
            md_path = build_bespoke_public_md(self.root / "exec_pub", self.rng, t0)
            check_executive_dashboard_invariants(
                self.sim, tdir, team, solo, silent, team, md_path, _CANARY_SECRET
            )
            check_unified_bridge_invariants(
                self.sim, tdir, solo, silent, team, scratch, _CANARY_SECRET
            )
