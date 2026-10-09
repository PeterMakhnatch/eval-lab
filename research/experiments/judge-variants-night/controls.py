"""Exercise the real changed webdev renderer/grader, one offline container.

No model keys are passed. Blank, off-brief and overflow controls must stop
before inference; the necessary-gates passing control must mask an unversioned
deployment, rather than fabricate a judge score. This is not an oracle.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from build import HERE, REPO, SCRATCH, WEBDEV, source

from evallab.task_variants import VariantExistsError, default_variants_root
from evallab.webdev_structural_gate import derive_webdev_structural_gate
from evallab.webdev_temp0_pin import derive_webdev_temp0_pin

IMAGE = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:6e30e61bdbe1504470cd1f705b120d317da9e7f57df3ba04a906db6b2e95e14d"
TASK = "dasyn_260630_00001"


def main() -> None:
    current = WEBDEV / "tasks" / TASK
    parent_source = source(WEBDEV, TASK)
    for derive in (derive_webdev_temp0_pin, derive_webdev_structural_gate):
        try:
            rec = derive(current, repo_root=REPO, parent_source=parent_source,
                         created_by="judge-variants-night")
            digest, task_name = rec.variant_digest, rec.task_name
        except VariantExistsError as exc:
            data = json.loads(Path(str(exc).removeprefix("lineage record already exists: ")).read_text())
            digest, task_name = data["variant_digest"], data["task_name"]
        slug, short = task_name.replace("/", "__"), digest.removeprefix("sha256:")[:12]
        record = Path("library/task-variants") / slug / f"{short}.json"
        current = default_variants_root(REPO) / slug / short
        parent_source = {"kind": "variant", "record": str(record)}
    root = SCRATCH / "browser-controls"
    root.mkdir(parents=True, exist_ok=True)
    tests = root / "tests"
    if tests.exists():
        shutil.rmtree(tests)  # Our own reproducible harness copy only.
    shutil.copytree(current / "tests", tests)
    fixtures = {
        "blank": "<html><body></body></html>",
        "hidden-keyword": '<html><body><h1>Unrelated page</h1><p style="display:none">Nexa Proteção</p></body></html>',
        "overflow": '<html><body><div style="width:3000px">Nexa Proteção</div></body></html>',
        "necessary-gates-pass": '<html><body><h1>Nexa Proteção</h1><p>Seguro Auto, Vida, Saúde, FAQ, WhatsApp</p></body></html>',
    }
    for name, html in fixtures.items():
        (root / name / "dist").mkdir(parents=True, exist_ok=True)
        (root / name / "dist/index.html").write_text(
            html.replace("<html>", '<html><head><meta charset="utf-8"></head>'))
        (root / name / "logs/verifier").mkdir(parents=True, exist_ok=True)
    commands = "set +e; " + "; ".join(
        f"mkdir -p /workspace; rm -rf /workspace/dist; cp -R /control/{name}/dist /workspace/dist; "
        f"rm -rf /logs/verifier; mkdir -p /logs/verifier; "
        f"python3 /tests/grade.py; cp /logs/verifier/result.json /control/{name}/result.json; "
        f"test ! -f /logs/verifier/structural_gates.json || cp /logs/verifier/structural_gates.json /control/{name}/gates.json"
        for name in fixtures
    )
    proc = subprocess.run([
        "docker", "run", "--rm", "--name", "mimo-judge-variants-browser-controls", "--network", "none",
        "--memory", "1536m", "--cpus", "2", "-e", "WEBDEV_GRADE_HTTP=1",
        "-v", f"{root}:/control", "-v", f"{tests}:/tests:ro", IMAGE, "bash", "-c", commands,
    ], capture_output=True, text=True, timeout=1200)
    rows = []
    for name in fixtures:
        rows.append({"control": name, "result": json.loads((root / name / "result.json").read_text()),
                     "gates": json.loads((root / name / "gates.json").read_text())
                     if (root / name / "gates.json").is_file() else None})
    report = {"evidence_tier": "MEASURED", "task": TASK, "task_package_digest": digest,
              "command": "uv run python research/experiments/judge-variants-night/controls.py",
              "docker": {"image": IMAGE, "network": "none", "containers": 1, "rm": True},
              "exit_code": proc.returncode, "model_calls": 0, "stdout": proc.stdout[-4000:],
              "stderr": proc.stderr[-2000:], "controls": rows}
    (HERE / "browser-controls.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
