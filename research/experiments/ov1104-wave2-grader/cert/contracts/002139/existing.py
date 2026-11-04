import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, "/testbed")
sys.path.insert(0, "/workspace/repo/lib")


class FakeNotFound(Exception):
    pass


class FakeGCSState:
    def __init__(self, scenario="success"):
        self.scenario = scenario
        self.calls = []


STATE = FakeGCSState()


class FakeBlob:
    def __init__(self, bucket_name, blob_name):
        self.bucket_name = bucket_name
        self.blob_name = blob_name

    def download_to_filename(self, filename):
        STATE.calls.append(("download_to_filename", self.bucket_name, self.blob_name, filename))
        if STATE.scenario == "notfound":
            raise FakeNotFound("missing")
        if STATE.scenario == "missingfile":
            return None
        with open(filename, "wb") as f:
            f.write(b"" if STATE.scenario == "zero" else b"payload-bytes\n")
        return None

    def exists(self):
        STATE.calls.append(("exists", self.bucket_name, self.blob_name))
        return STATE.scenario != "notfound"


class FakeBucket:
    def __init__(self, bucket_name):
        self.bucket_name = bucket_name

    def blob(self, blob_name):
        STATE.calls.append(("blob", self.bucket_name, blob_name))
        return FakeBlob(self.bucket_name, blob_name)


class FakeStorageClient:
    def __init__(self, project="UNSET"):
        STATE.calls.append(("Client", project))

    def bucket(self, bucket_name):
        STATE.calls.append(("bucket", bucket_name))
        return FakeBucket(bucket_name)


def install_fake_google():
    google = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    storage = types.ModuleType("google.cloud.storage")
    api_core = types.ModuleType("google.api_core")
    exceptions = types.ModuleType("google.api_core.exceptions")
    storage.Client = FakeStorageClient
    exceptions.NotFound = FakeNotFound
    cloud.storage = storage
    api_core.exceptions = exceptions
    google.cloud = cloud
    google.api_core = api_core
    sys.modules["google"] = google
    sys.modules["google.cloud"] = cloud
    sys.modules["google.cloud.storage"] = storage
    sys.modules["google.api_core"] = api_core
    sys.modules["google.api_core.exceptions"] = exceptions


install_fake_google()

import bb.data  # noqa: E402
import bb.fetch2  # noqa: E402
from bb.fetch2 import gcp  # noqa: E402


class TestGCPFetcherContract(unittest.TestCase):
    def setUp(self):
        global STATE
        STATE = FakeGCSState("success")
        self.tmpdir = tempfile.TemporaryDirectory()
        self.d = bb.data.init()
        self.d.setVar("DL_DIR", self.tmpdir.name)
        self.d.setVar("PREMIRRORS", "")
        self.d.setVar("MIRRORS", "")
        self.d.setVar("BB_STRICT_CHECKSUM", "ignore")
        self.d.setVar("PN", "gcp-contract")
        for m in bb.fetch2.methods:
            if isinstance(m, gcp.GCP):
                m.gcp_client = None

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_supports_checksum_and_urldata_init(self):
        inst = gcp.GCP()
        self.assertIsNone(inst.gcp_client)
        url1 = "gs://my-bucket/dir/archive.tar.gz"
        f1 = bb.fetch2.Fetch([url1], self.d)
        ud1 = f1.ud[url1]
        self.assertTrue(inst.supports(ud1, self.d))
        self.assertTrue(inst.recommends_checksum(ud1))
        self.assertEqual(ud1.basename, "archive.tar.gz")
        self.assertEqual(ud1.localfile, "archive.tar.gz")

        http_ud = bb.fetch2.FetchData("http://example.com/a.tar.gz", self.d)
        self.assertFalse(inst.supports(http_ud, self.d))

        url2 = "gs://my-bucket/dir/archive;downloadfilename=custom.tar.gz"
        f2 = bb.fetch2.Fetch([url2], self.d)
        ud2 = f2.ud[url2]
        self.assertEqual(ud2.basename, "custom.tar.gz")
        self.assertEqual(ud2.localfile, "custom.tar.gz")

    def test_download_and_checkstatus_success_logging_and_client_reuse(self):
        url = "gs://my-bucket/sub/file.tar.gz"
        f = bb.fetch2.Fetch([url], self.d)
        ud = f.ud[url]
        f.localpath(url)

        with patch.object(gcp.logger, "debug2") as mock_debug2:
            ret_dl = ud.method.download(ud, self.d)
            self.assertTrue(ret_dl)
            mock_debug2.assert_called_with(f"Trying to download gs://my-bucket/sub/file.tar.gz to {ud.localpath}")

        self.assertTrue(os.path.exists(ud.localpath))
        self.assertEqual(STATE.calls.count(("Client", None)), 1)
        self.assertIn(("bucket", "my-bucket"), STATE.calls)
        self.assertIn(("blob", "my-bucket", "sub/file.tar.gz"), STATE.calls)

        with patch.object(gcp.logger, "debug2") as mock_debug2:
            ret_cs = ud.method.checkstatus(f, ud, self.d)
            self.assertTrue(ret_cs)
            mock_debug2.assert_called_with("Checking status of gs://my-bucket/sub/file.tar.gz")

        self.assertEqual(STATE.calls.count(("Client", None)), 1)
        self.assertIn(("exists", "my-bucket", "sub/file.tar.gz"), STATE.calls)

    def test_checkstatus_initializes_client_when_none_and_fails_when_missing(self):
        url = "gs://my-bucket/sub/missing.tar.gz"
        f = bb.fetch2.Fetch([url], self.d)
        ud = f.ud[url]
        ud.method.gcp_client = None
        STATE.scenario = "notfound"

        with self.assertRaises(bb.fetch2.FetchError) as ctx:
            ud.method.checkstatus(f, ud, self.d)
        self.assertIn("gs://my-bucket/sub/missing.tar.gz", str(ctx.exception))
        self.assertEqual(STATE.calls.count(("Client", None)), 1)
        self.assertIn(("exists", "my-bucket", "sub/missing.tar.gz"), STATE.calls)

    def test_download_error_conditions_and_network_checks(self):
        url = "gs://my-bucket/sub/item.tar.gz"
        f = bb.fetch2.Fetch([url], self.d)
        ud = f.ud[url]
        f.localpath(url)

        STATE.scenario = "notfound"
        with self.assertRaises(bb.fetch2.FetchError):
            ud.method.download(ud, self.d)

        STATE.scenario = "missingfile"
        if os.path.exists(ud.localpath):
            os.remove(ud.localpath)
        with self.assertRaises(bb.fetch2.FetchError) as ctx_missing:
            ud.method.download(ud, self.d)
        self.assertIn("gs://my-bucket/sub/item.tar.gz", str(ctx_missing.exception))

        STATE.scenario = "zero"
        with self.assertRaises(bb.fetch2.FetchError) as ctx_zero:
            ud.method.download(ud, self.d)
        self.assertFalse(os.path.exists(ud.localpath))
        self.assertIn("gs://my-bucket/sub/item.tar.gz", str(ctx_zero.exception))

        self.d.setVar("BB_NO_NETWORK", "1")
        STATE.calls.clear()
        with self.assertRaises(bb.fetch2.NetworkAccess):
            ud.method.download(ud, self.d)
        with self.assertRaises(bb.fetch2.NetworkAccess):
            ud.method.checkstatus(f, ud, self.d)
        self.assertEqual(STATE.calls, [])


if __name__ == "__main__":
    unittest.main()
