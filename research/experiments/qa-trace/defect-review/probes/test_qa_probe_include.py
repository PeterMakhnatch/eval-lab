"""QA Trace defect probe for format-code-task-000169 (hpccm recipe includes).

The prompt requires that included recipes contribute "building blocks,
primitives, and values needed later by the including recipe", and that "the
examples should demonstrate reusing a sibling recipe file". The hidden tests
include only primitives and scalar values and never build an example. The
object-reuse fixture is a candidate additional semantic test. The example
expectations are review inputs for the specific submitted/reference recipes,
not a requirement to use those filenames or Ubuntu versions in another fix.
"""

import inspect
import json
import os
import shutil
import tempfile
import unittest

import hpccm  # noqa: F401  (recipes call hpccm.include)
from hpccm.recipe import recipe


def _recipe(path):
    """Select the supported signature before executing; never retry a failed recipe."""
    kwargs = {}
    if "raise_exceptions" in inspect.signature(recipe).parameters:
        kwargs["raise_exceptions"] = True
    return recipe(path, **kwargs)


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
        examples = json.loads(os.environ.get("PROBE_EXAMPLE_EXPECTATIONS", "{}"))
        self.assertTrue(examples, "the change adds no example that includes a sibling recipe")
        for name, expected_fragments in examples.items():
            with self.subTest(recipe=name):
                out = _recipe(os.path.join("/testbed", name))
                for fragment in expected_fragments:
                    self.assertIn(fragment, out, name)
