"""QA Trace defect probe for format-code-task-000169 (hpccm recipe includes).

The prompt requires that included recipes contribute "building blocks,
primitives, and values needed later by the including recipe", and that "the
examples should demonstrate reusing a sibling recipe file". The hidden tests
include only primitives and scalar values and never build an example. These
two checks cover what they leave out.
"""

import os
import shutil
import tempfile
import unittest

import hpccm  # noqa: F401  (recipes call hpccm.include)
from hpccm.recipe import recipe


def _recipe(path):
    """Build a recipe, raising on errors when this hpccm version supports it."""
    try:
        return recipe(path, raise_exceptions=True)
    except TypeError:
        return recipe(path)


class Test_qa_probe_include(unittest.TestCase):
    def test_included_building_block_object_is_reused_by_the_parent(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "common.py"), "w") as f:
                f.write(
                    "Stage0 += baseimage(image='ubuntu:20.04')\n"
                    "compiler = gnu()\n"
                    "Stage0 += compiler\n"
                )
            main = os.path.join(tmp, "main.py")
            with open(main, "w") as f:
                f.write(
                    "hpccm.include('common.py')\n"
                    "Stage0 += packages(ospackages=['probe-' + type(compiler).__name__])\n"
                )
            out = _recipe(main)
            self.assertIn("probe-gnu", out)
            self.assertIn("g++", out)
        finally:
            shutil.rmtree(tmp)

    def test_examples_added_by_the_change_build(self):
        names = os.environ.get("PROBE_EXAMPLES", "").split()
        self.assertTrue(names, "the change adds no example recipe")
        for name in names:
            out = _recipe(os.path.join("/testbed", name))
            self.assertTrue(out.strip(), name)
