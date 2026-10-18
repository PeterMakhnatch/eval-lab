from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from evallab import mimo_runtime_repair as producer
from evallab.verifier_repair import RepairTest, build_patch_appendix


IMPORT = "from configuration_components.install_plan import build_install_plan\n"


def test_missing_requested_api_fails_each_pytest_case_instead_of_collection(tmp_path: Path) -> None:
    source = (
        IMPORT
        + 'import pytest\n\n'
        + '@pytest.mark.parametrize("flag", [False, True])\n'
        + 'def test_requested_flag(flag):\n'
        + '    """Preserve the existing case documentation and parameters."""\n'
        + '    assert build_install_plan()["flag"] is flag\n\n'
        + 'def test_default():\n'
        + '    assert build_install_plan() == {}\n'
    )
    package = tmp_path / "configuration_components"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "install_plan.py").write_text("")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_plan.py").write_text(
        producer._defer_install_plan_import(source, expected_tests=2)
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", "pytest.ini", "--junitxml=cases.xml", "test_plan.py"],
        cwd=tmp_path,
        env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTEST_ADDOPTS=""),
        capture_output=True, text=True, check=False,
    )
    cases = list(ET.parse(tmp_path / "cases.xml").iter("testcase"))
    assert result.returncode == 1
    assert [case.get("name") for case in cases] == [
        "test_requested_flag[False]", "test_requested_flag[True]", "test_default",
    ]
    assert all(case.find("failure") is not None for case in cases)
    assert all(case.find("error") is None and case.find("skipped") is None for case in cases)
    assert all("cannot import name 'build_install_plan'" in ET.tostring(case).decode() for case in cases)


def test_missing_requested_api_runs_named_unittest_not_loader_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("configuration_components")
    package.__path__ = []
    implementation = types.ModuleType("configuration_components.install_plan")
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, implementation.__name__, implementation)
    source = (
        "import unittest\n" + IMPORT + "\n"
        + "class PlanTests(unittest.TestCase):\n"
        + "    def test_requested_flag(self):\n"
        + "        for flag in (False, True):\n"
        + "            with self.subTest(flag=flag):\n"
        + "                self.assertEqual(build_install_plan()['flag'], flag)\n"
    )
    module = types.ModuleType("bounded_collection")
    exec(producer._defer_install_plan_import(source, expected_tests=1), module.__dict__)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    assert suite.countTestCases() == 1
    result = unittest.TestResult()
    suite.run(result)
    assert result.testsRun == 1 and not result.failures and not result.skipped
    assert len(result.errors) == 1
    case, error = result.errors[0]
    assert case.id() == "bounded_collection.PlanTests.test_requested_flag"
    assert "cannot import name 'build_install_plan'" in error
    assert "_FailedTest" not in error


def test_collection_patch_changes_only_both_added_module_import_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        next(iter(producer.TEST_PATHS_002486)): IMPORT + "\n" + "\n".join(
            f"def test_plan_{index}():\n    assert build_install_plan() == {{}}\n"
            for index in range(9)
        ),
        "tests/test_repair_browser_opt_in.py": (
            "import unittest\n" + IMPORT + "\n"
            + "class PlanTests(unittest.TestCase):\n"
            + "    def test_plan(self):\n        self.assertEqual(build_install_plan(), {})\n"
        ),
    }
    untouched = build_patch_appendix([
        RepairTest(id="fixture", repo_path="fixtures/input.json", code='{"flag": false}\n'),
    ])
    command = (
        "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
        "new file mode 100755\n--- /dev/null\n+++ b/mimo_test_command.sh\n"
        "@@ -0,0 +1,1 @@\n+python -m pytest test_plan.py\n"
    )
    modules = [build_patch_appendix([RepairTest(id="test", repo_path=path, code=source)])
               for path, source in sources.items()]
    original = (untouched + modules[0] + command + modules[1]).encode()
    monkeypatch.setattr(producer, "PARENT_002486_PATCH_SHA256", hashlib.sha256(original).hexdigest())
    changed = producer.collection_patch_002486(original)
    expected_modules = [build_patch_appendix([RepairTest(
        id="test", repo_path=path,
        code=producer._defer_install_plan_import(source, expected_tests=producer.TEST_PATHS_002486[path]),
    )]) for path, source in sources.items()]
    assert changed == (untouched + expected_modules[0] + command + expected_modules[1]).encode()
    assert changed != original


def test_collection_patch_refuses_a_different_frozen_preimage() -> None:
    with pytest.raises(ValueError, match="exact frozen002486 test patch"):
        producer.collection_patch_002486(b"not the frozen test patch")


@pytest.mark.parametrize("source,count", [
    (IMPORT + "\ndef helper():\n    pass\n", 1),
    (IMPORT + "\ndef test_plan():\n    pass\n", 2),
    ("def test_plan():\n    pass\n", 1),
])
def test_import_relocation_refuses_a_changed_case_inventory(source: str, count: int) -> None:
    with pytest.raises(ValueError, match="application import/test boundary changed"):
        producer._defer_install_plan_import(source, expected_tests=count)
