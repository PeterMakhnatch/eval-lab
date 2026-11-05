"""Ground: all-run continuation, named-example propagation, and supplied argv flags."""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, '/testbed')
from xdoctest import doctest_module


class PublicDoctest(unittest.TestCase):
    def module(self, directory):
        marker = os.path.join(directory, 'events')
        path = os.path.join(directory, 'public_examples.py')
        with open(path, 'w') as handle:
            for name, expected in [('broken', 9), ('good', 2), ('also_broken', 8), ('last', 2)]:
                handle.write('def %s():\n    """\n    Example:\n' % name)
                handle.write('        >>> with open(%r, "a") as h: n = h.write(%r)\n' % (marker, name + '|'))
                handle.write('        >>> 1 + 1\n        %d\n    """\n    pass\n\n' % expected)
        return path, marker

    def invoke(self, path, command=None, argv=None, process=None):
        output = io.StringIO()
        with patch.object(sys, 'argv', process or ['unrelated-process']), contextlib.redirect_stdout(output):
            value = doctest_module(path, command=command, argv=argv)
        return value, output.getvalue()

    def test_all_continues_and_summarizes_all_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path, marker = self.module(directory)
            _, text = self.invoke(path, 'all', ['--quiet'])
            with open(marker) as handle:
                self.assertEqual(sorted(handle.read().strip('|').split('|')), ['also_broken', 'broken', 'good', 'last'])
            self.assertRegex(text, r'2\s*/\s*4\s+passed')
            self.assertIn('broken', text)
            self.assertIn('also_broken', text)

    def test_named_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.module(directory)
            with self.assertRaises(Exception):
                self.invoke(path, 'broken', [])

    def test_supplied_argv_controls_verbosity_and_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path, marker = self.module(directory)
            _, quiet = self.invoke(path, argv=['all', '--quiet'], process=['host', '--verbose'])
            os.remove(marker)
            _, loud = self.invoke(path, argv=['all', '--verbose'], process=['host', '--quiet'])
            with open(marker) as handle:
                self.assertEqual(len(handle.read().strip('|').split('|')), 4)
            self.assertRegex(quiet, r'2\s*/\s*4\s+passed')
            self.assertRegex(loud, r'2\s*/\s*4\s+passed')
            self.assertNotEqual(quiet, loud)

    def test_leading_flag_is_not_a_test_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path, marker = self.module(directory)
            _, text = self.invoke(path, argv=['--verbose'])
            self.assertFalse(os.path.exists(marker))
            self.assertIn('broken', text)
            self.assertIn('last', text)

    def test_supplied_style_overrides_process_style(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'free_examples.py')
            marker = os.path.join(directory, 'freeform-ran')
            with open(path, 'w') as handle:
                handle.write('def free_example():\n    """\n    >>> with open(%r, "w") as h: n = h.write("ran")\n    """\n    pass\n' % marker)
            self.invoke(path, 'all', ['--freeform'], process=['host', '--google'])
            with open(marker) as handle:
                self.assertEqual(handle.read(), 'ran')


if __name__ == '__main__':
    unittest.main()
