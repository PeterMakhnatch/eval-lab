#!/usr/bin/env python3
"""Rule tests for scan_grader_surface.py on two hand-written toy packages.

Run: python3 test_scan_grader_surface.py
Nothing outside a temporary directory is touched and nothing is executed.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCANNER = HERE / "scan_grader_surface.py"


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def mimo_like(root: Path) -> None:
    write(root, "task.toml", '[environment]\nworkdir = "/testbed"\n')
    write(root, "environment/Dockerfile", "FROM python:3.12\nWORKDIR /testbed\n")
    write(root, "tests/test_command.sh", "python -m pytest tests/test_thing.py -v\n")
    write(root, "tests/test.sh", """#!/bin/bash
CWD=/testbed
cd "$CWD" || exit 1
if ! git apply /tests/test.patch; then exit 1; fi
timeout 60 sh -c "$(cat /tests/test_command.sh)"
RC=$?
if [ $RC -eq 0 ]; then echo 1 >/logs/verifier/reward.txt; else echo 0 >/logs/verifier/reward.txt; fi
""")
    write(root, "tests/test.patch", """diff --git a/tests/test_thing.py b/tests/test_thing.py
--- a/tests/test_thing.py
+++ b/tests/test_thing.py
@@ -1 +1 @@
-assert old()
+assert new()
""")
    write(root, "environment/setup/setup.sh", "#!/bin/bash\ntouch /var/lib/mimo/ready\n")


def separate_like(root: Path) -> None:
    write(root, "task.toml", '[environment]\nworkdir = "/app"\n')
    write(root, "environment/Dockerfile", "FROM python:3.12\nWORKDIR /app\n")
    write(root, "environment/golden/sa_answers.csv", "a,b\n1,2\n")
    write(root, "tests/Dockerfile", "FROM python:3.12\nCOPY . /tests/\n")
    write(root, "tests/test.sh", """#!/bin/bash
pytest --ctrf /logs/verifier/ctrf.json /tests/test_out.py -rA
if [ $? -eq 0 ]; then echo 1 >/logs/verifier/reward.txt; else echo 0 >/logs/verifier/reward.txt; fi
""")
    write(root, "tests/test_out.py", """import openpyxl
OUTPUT_PATH = "/app/out.xlsx"
EXPECTED_ROWS = 3

def test_shape():
    wb = openpyxl.load_workbook(OUTPUT_PATH)
    assert len(wb.sheetnames) >= 2
    assert sum(1 for ws in wb for row in ws.iter_rows() for c in row if c.value) >= 30
""")
    write(root, "tests/test.patch", "")


def cmd_wrapper_like(root: Path) -> None:
    write(root, "task.toml", '[environment]\nworkdir = "/testbed"\n')
    write(root, "environment/Dockerfile", "FROM python:3.12\nWORKDIR /testbed\n")
    write(root, "tests/test.sh", """#!/bin/bash
cd /testbed
sh -c "$(cat /testbed/mimo_test_command.sh)"
RC=$?
if [ $RC -eq 0 ]; then echo 1 >/logs/verifier/reward.txt; else echo 0 >/logs/verifier/reward.txt; fi
""")
    write(root, "tests/test.patch", """diff --git a/mimo_test_command.sh b/mimo_test_command.sh
new file mode 100755
--- /dev/null
+++ b/mimo_test_command.sh
@@ -0,0 +1,2 @@
+#!/bin/bash
+python -m pytest tests/test_x.py
""")


class ScannerRules(unittest.TestCase):
    def scan(self, builder) -> dict:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "pkg"
            root.mkdir()
            builder(root)
            out = subprocess.run([sys.executable, str(SCANNER), str(root)],
                                 capture_output=True, text=True, check=True)
            return json.loads(out.stdout)

    def test_same_container_hook_surfaces(self) -> None:
        d = self.scan(mimo_like)
        flags = d["class_flags"]
        self.assertTrue(flags["framework_config_injection"])
        self.assertTrue(flags["runner_exit_status_forgery"])
        # modified existing test module is not in the (empty) reset list
        self.assertTrue(flags["test_file_sabotage"])
        self.assertFalse(flags["hidden_answer_read"])
        for cls in ("framework_config_injection", "runner_exit_status_forgery",
                    "test_file_sabotage"):
            for item in flags[cls]:
                self.assertTrue(item["file"])
                self.assertGreaterEqual(item["line"], 1)
                self.assertTrue(item["rule"])

    def test_separate_verifier_artifact_surfaces(self) -> None:
        d = self.scan(separate_like)
        flags = d["class_flags"]
        self.assertFalse(flags["framework_config_injection"], "separate verifier cannot load agent conftest")
        self.assertFalse(flags["runner_exit_status_forgery"])
        self.assertFalse(flags["test_file_sabotage"])
        self.assertTrue(flags["hardcoded_expected_output"])
        self.assertTrue(flags["synthetic_artifact_shape_fill"])
        self.assertTrue(flags["hidden_answer_read"])

    def test_command_wrapper_in_workdir(self) -> None:
        d = self.scan(cmd_wrapper_like)
        self.assertTrue(d["class_flags"]["grader_tamper"])
        for item in d["class_flags"]["grader_tamper"]:
            self.assertTrue(item["file"])
            self.assertGreaterEqual(item["line"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
