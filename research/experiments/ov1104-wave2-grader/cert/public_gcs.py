"""Instruction-derived public API check. No implementation-module names or source inspection."""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, '/workspace/repo/lib')


class GoogleAPICallError(Exception):
    pass


class NotFound(GoogleAPICallError):
    pass


STATE = {'scenario': 'success', 'calls': []}


class Blob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name

    def download_to_filename(self, filename):
        STATE['calls'].append(('download', self.bucket, self.name))
        if STATE['scenario'] == 'notfound':
            raise NotFound('missing')
        if STATE['scenario'] == 'missingfile':
            return
        with open(filename, 'wb') as handle:
            handle.write(b'' if STATE['scenario'] == 'zero' else b'public-instruction-payload')

    def exists(self):
        STATE['calls'].append(('exists', self.bucket, self.name))
        return STATE['scenario'] != 'notfound'


class Bucket:
    def __init__(self, name):
        self.name = name

    def blob(self, name):
        return Blob(self.name, name)


class Client:
    def __init__(self, *args, **kwargs):
        STATE['calls'].append(('client',))

    def bucket(self, name):
        return Bucket(name)


modules = {name: types.ModuleType(name) for name in ['google', 'google.cloud', 'google.cloud.storage',
    'google.api_core', 'google.api_core.exceptions']}
modules['google.cloud.storage'].Client = Client
modules['google.api_core.exceptions'].NotFound = NotFound
modules['google.api_core.exceptions'].GoogleAPICallError = GoogleAPICallError
modules['google'].cloud = modules['google.cloud']
modules['google'].api_core = modules['google.api_core']
modules['google.cloud'].storage = modules['google.cloud.storage']
modules['google.api_core'].exceptions = modules['google.api_core.exceptions']
sys.modules.update(modules)

import bb.data
import bb.fetch2


class PublicGCS(unittest.TestCase):
    def prepare(self, directory, scenario='success', suffix='pkg-1.0.tar.gz', no_network=False):
        STATE.update(scenario=scenario, calls=[])
        d = bb.data.init()
        for key, value in {'DL_DIR': directory, 'PREMIRRORS': '', 'MIRRORS': '',
            'BB_STRICT_CHECKSUM': 'ignore', 'PN': 'public-gcs', 'BB_NO_NETWORK': '1' if no_network else '0'}.items():
            d.setVar(key, value)
        url = 'gs://example-bucket/releases/' + suffix
        return bb.fetch2.Fetch([url], d), url

    def test_success_default_and_explicit_filename(self):
        for suffix, expected in [('pkg-1.0.tar.gz', 'pkg-1.0.tar.gz'),
                                 ('pkg;downloadfilename=source.tar.gz', 'source.tar.gz')]:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                fetch, url = self.prepare(directory, suffix=suffix)
                target = fetch.localpath(url)
                self.assertEqual(os.path.basename(target), expected)
                fetch.download()
                with open(target, 'rb') as handle:
                    self.assertEqual(handle.read(), b'public-instruction-payload')
                fetch.checkstatus()
                obj = 'releases/' + suffix.split(';')[0]
                self.assertIn(('download', 'example-bucket', obj), STATE['calls'])
                self.assertIn(('exists', 'example-bucket', obj), STATE['calls'])

    def test_notfound_missing_output_and_empty_output(self):
        for scenario in ['notfound', 'missingfile', 'zero']:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                fetch, url = self.prepare(directory, scenario=scenario)
                target = fetch.localpath(url)
                with self.assertRaises(bb.fetch2.FetchError):
                    fetch.download()
                if scenario == 'zero':
                    self.assertFalse(os.path.exists(target))

    def test_missing_blob_and_network_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            fetch, _ = self.prepare(directory, scenario='notfound')
            with self.assertRaises(bb.fetch2.FetchError):
                fetch.checkstatus()
        for operation in ['download', 'checkstatus']:
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                fetch, _ = self.prepare(directory, no_network=True)
                with self.assertRaises(bb.fetch2.NetworkAccess):
                    getattr(fetch, operation)()
                self.assertEqual(STATE['calls'], [])


if __name__ == '__main__':
    unittest.main()
