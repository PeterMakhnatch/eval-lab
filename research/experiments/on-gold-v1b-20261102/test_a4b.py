"""Offline tests (synthetic fixtures, no Modal, no model) for the A4B filtering, environment and aggregation helpers."""

from __future__ import annotations

import base64
import io
import sys
import tarfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from a4b_synth import (  # noqa: E402
    drop_tests,
    failure_blocks,
    list_test_methods,
    parse_verdicts,
    prepare_code,
)
from aggregate import a2_counts, eval_pool, final_record, paired_lists  # noqa: E402
from envspec import derive_env_spec  # noqa: E402

SUITE = '''import unittest


class T(unittest.TestCase):
    def test_a(self):
        self.assertEqual(1, 1)

    @unittest.skip("x")
    def test_b(self):
        self.assertEqual(1, 2)

    def test_c(self):
        self.assertEqual(1, 2)

    def helper(self):
        return 3

    def test_d(self):
        self.assertEqual(self.helper(), 3)


if __name__ == "__main__":
    unittest.main()
'''

LOG = """collected hidden suite
test_hidden_one (tests.test_x.Hidden.test_hidden_one) ... ok
----------------------------------------------------------------------
Ran 1 test in 0.1s

OK
test_a (__main__.T.test_a) ... ok
test_c (__main__.T.test_c) ... FAIL
test_d (__main__.T.test_d) ... some printed line
ERROR
test_e (__main__.T.test_e) ... ok

======================================================================
FAIL: test_c (__main__.T.test_c)
----------------------------------------------------------------------
Traceback (most recent call last):
AssertionError: 1 != 2

======================================================================
ERROR: test_d (__main__.T.test_d)
----------------------------------------------------------------------
Traceback (most recent call last):
KeyError: 'x'
"""


def test_verdicts_ignore_hidden_suite_and_read_split_lines() -> None:
    v = parse_verdicts(LOG)
    assert v == {"test_a": "ok", "test_c": "FAIL", "test_d": "ERROR", "test_e": "ok"}


def test_drop_tests_removes_exact_spans_and_keeps_the_rest_parseable() -> None:
    kept = drop_tests(SUITE, {"test_b", "test_c"})
    names = [m[0] for m in list_test_methods(kept)]
    assert names == ["test_a", "test_d"]
    assert "helper" in kept and "unittest.skip" not in kept  # decorator of the dropped method goes with it
    compile(kept, "<kept>", "exec")


def test_failure_blocks_show_only_requested_tests() -> None:
    shown = failure_blocks(LOG, {"test_d"})
    assert "KeyError" in shown and "AssertionError" not in shown


def test_prepare_code_forces_verbose_runner_once() -> None:
    out = prepare_code(SUITE)
    assert out.count("unittest.main(") == 1 and "verbosity=2" in out
    assert "unittest.main(verbosity=2)" in prepare_code("import unittest\n")


def _patch_adding(name: str, body: str) -> str:
    lines = body.splitlines()
    return (f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{ln}\n" for ln in lines))


def _tarball_patch(command: str) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = command.encode()
        info = tarfile.TarInfo("env/test_command.sh")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return _patch_adding("mimo_build_env.tar.gz.b64", base64.b64encode(buf.getvalue()).decode())


def test_env_spec_replays_environment_lines_and_picks_explicit_interpreter() -> None:
    cmd = ("#!/bin/bash\nexport LANG=C.UTF-8\nsource /opt/venv/bin/activate\ncd /workspace/repo\n"
           "/opt/venv/bin/python3.9 -m pytest tests/test_a.py -x\n")
    spec = derive_env_spec(_tarball_patch(cmd))
    assert spec.workdir == "/workspace/repo" and spec.source == "build_env"
    assert "source /opt/venv/bin/activate" in spec.hook_command
    assert spec.hook_command.rstrip().endswith("/opt/venv/bin/python3.9")
    assert spec.python_version == "3"


def test_env_spec_defaults_without_runner_files() -> None:
    spec = derive_env_spec(_patch_adding("tests/test_a.py", "def test_a():\n    pass"))
    assert spec.workdir == "/testbed" and spec.hook_command.rstrip().endswith("python3")


def test_env_spec_detects_python27_from_mimo_command() -> None:
    spec = derive_env_spec(_patch_adding("mimo_test_command.sh", "cd /testbed\npyenv global 2.7.18\npython -m pytest t.py"))
    assert spec.python_version == "2.7"
    assert "pyenv global 2.7.18" in spec.hook_command and spec.hook_command.rstrip().endswith("python")


def test_final_record_prefers_round2_and_flags_cap_stop() -> None:
    r1, r2 = {"status": "a"}, {"status": "b"}
    assert final_record(r1, r2) == {"status": "b", "round2_measured": True}
    assert final_record(r1, None) == {"status": "a", "round2_measured": False}
    assert final_record(None, None) is None


def test_paired_lists_skip_unmeasured_and_ungraded_mutants() -> None:
    measured = {"round2_measured": True, "paired_eval_base_accepted": [
        {"arm1_killed": False, "arm2_killed": True}, {"arm1_killed": None, "arm2_killed": True},
        {"arm1_killed": True, "arm2_killed": True}]}
    unmeasured = {"round2_measured": False, "paired_eval_base_accepted": [{"arm1_killed": True, "arm2_killed": True}]}
    a, b = paired_lists([measured, unmeasured])
    assert (a, b) == ([True, True], [False, True])


def test_eval_pool_sums_shipped_round_counts() -> None:
    recs = [{"counts": {"eval_base_accepted": 3, "eval_base_accepted_killed_arm2": 2}},
            {"counts": {"eval_base_accepted": 1, "eval_base_accepted_killed_arm2": 0}}, {}]
    assert eval_pool(recs) == {"k": 2, "n": 4}


def test_weak_grader_counts_require_a_killed_survivor_and_an_accepted_test() -> None:
    recs = [
        {"task_id": "t1", "is_weak": True, "accepted_test": True,
         "a2_survivors": [{"killed": True}, {"killed": False}, {"killed": None}]},
        {"task_id": "t2", "is_weak": True, "accepted_test": True, "a2_survivors": [{"killed": False}]},
        {"task_id": "t3", "is_weak": True, "accepted_test": False, "a2_survivors": []},
        {"task_id": "t4", "is_weak": False, "accepted_test": True, "a2_survivors": [{"killed": True}]},
    ]
    out = a2_counts(recs)
    assert out["a2_survivors_exercised"] == 4 and out["a2_survivors_killed"] == 2  # None = not exercised
    assert out["weak_tasks_with_accepted_test"] == 2 and out["weak_demonstrated_ids"] == ["t1"]


@pytest.mark.parametrize("bad", ["def test_x(:\n  pass"])
def test_unparsable_candidate_raises_so_the_driver_can_record_it(bad: str) -> None:
    with pytest.raises(SyntaxError):
        list_test_methods(bad)
