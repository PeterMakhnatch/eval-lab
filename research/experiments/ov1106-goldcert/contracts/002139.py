"""Ground: instruction's bb.fetch2.Fetch session, filenames, errors, and network checks."""
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
        self.bucket, self.name = bucket, name

    def download_to_filename(self, filename):
        STATE['calls'].append(('download', self.bucket, self.name))
        if STATE['scenario'] == 'notfound':
            raise NotFound('object missing')
        if STATE['scenario'] == 'missingfile':
            return
        with open(filename, 'wb') as handle:
            handle.write(b'' if STATE['scenario'] == 'zero' else b'public-payload')

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
        pass

    def bucket(self, name):
        return Bucket(name)


modules = {name: types.ModuleType(name) for name in ('google', 'google.cloud',
    'google.cloud.storage', 'google.api_core', 'google.api_core.exceptions')}
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


class PublicFetch(unittest.TestCase):
    def prepare(self, directory, scenario='success', suffix='pkg-1.0.tar.gz', blocked=False):
        STATE.update(scenario=scenario, calls=[])
        data = bb.data.init()
        for key, value in {'DL_DIR': directory, 'PREMIRRORS': '', 'MIRRORS': '',
            'BB_STRICT_CHECKSUM': 'ignore', 'PN': 'public-gcs',
            'BB_NO_NETWORK': '1' if blocked else '0'}.items():
            data.setVar(key, value)
        url = 'gs://example-bucket/releases/' + suffix
        return bb.fetch2.Fetch([url], data), url

    def test_download_filenames_path_and_status(self):
        for suffix, expected in [('pkg-1.0.tar.gz', 'pkg-1.0.tar.gz'),
                                 ('pkg;downloadfilename=source.tar.gz', 'source.tar.gz')]:
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                fetch, url = self.prepare(directory, suffix=suffix)
                target = fetch.localpath(url)
                self.assertEqual(os.path.basename(target), expected)
                fetch.download()
                with open(target, 'rb') as handle:
                    self.assertEqual(handle.read(), b'public-payload')
                fetch.checkstatus()
                name = 'releases/' + suffix.split(';')[0]
                self.assertIn(('download', 'example-bucket', name), STATE['calls'])
                self.assertIn(('exists', 'example-bucket', name), STATE['calls'])

    def test_notfound_missing_and_empty_download(self):
        for scenario in ('notfound', 'missingfile', 'zero'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                fetch, url = self.prepare(directory, scenario)
                target = fetch.localpath(url)
                with self.assertRaises(bb.fetch2.FetchError):
                    fetch.download()
                if scenario == 'zero':
                    self.assertFalse(os.path.exists(target))

    def test_missing_status_and_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            fetch, _ = self.prepare(directory, 'notfound')
            with self.assertRaises(bb.fetch2.FetchError):
                fetch.checkstatus()
        for operation in ('download', 'checkstatus'):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                fetch, _ = self.prepare(directory, blocked=True)
                with self.assertRaises(bb.fetch2.NetworkAccess):
                    getattr(fetch, operation)()
                self.assertEqual(STATE['calls'], [])


if __name__ == '__main__':
    unittest.main()
