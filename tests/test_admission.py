"""Admission boundaries use isolated Git remotes and scripted HTTP responses."""
from __future__ import annotations

import io
import json
import subprocess
import urllib.error

import pytest

from evallab import admission


def test_gated_download_refuses_without_disclosing_response(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "private-credential")

    def forbidden(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 403, "secret-signed-url", {}, io.BytesIO(b"private response"))

    monkeypatch.setattr(admission.urllib.request, "urlopen", forbidden)
    with pytest.raises(ValueError, match="HTTP 403") as error:
        admission.access("example/gated", "a" * 40, "data.parquet")
    assert "private" not in str(error.value)
    assert "secret" not in str(error.value)


def test_access_requires_actual_nonempty_bytes(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr(admission.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b""))
    with pytest.raises(ValueError, match="no data bytes"):
        admission.access("example/data", "a" * 40, "data.jsonl")
    monkeypatch.setattr(admission.urllib.request, "urlopen", lambda *a, **kw: io.BytesIO(b"real data"))
    receipt = admission.access("example/data", "a" * 40, "data.jsonl")
    assert receipt["downloaded_bytes"] == 9
    assert receipt["prefix_sha256"] == admission.hashlib.sha256(b"real data").hexdigest()


def test_access_requires_credentials_and_immutable_revision(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(ValueError, match="HF_TOKEN"):
        admission.access("example/data", "a" * 40, "data.jsonl")
    with pytest.raises(ValueError, match="40-hex"):
        admission.access("example/data", "main", "data.jsonl")


def run(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


@pytest.fixture
def registered(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    remote = tmp_path / "remote.git"
    run(tmp_path, "init", "--bare", str(remote))
    run(root, "init", "-b", "topic")
    run(root, "config", "user.email", "test@example.invalid")
    run(root, "config", "user.name", "Admission test")
    run(root, "remote", "add", "origin", str(remote))
    source = root / "src/evallab"
    source.mkdir(parents=True)
    (source / "scanner.py").write_text("threshold = 1\n")
    precedent = root / admission.EXPERIMENT
    precedent.mkdir(parents=True)
    (precedent / "label-map.json").write_text('{}\n')
    (precedent / "PREREG.md").write_text('Frozen\n')
    registration = root / "research/experiments/admission/freeze.json"
    admission.save(registration, {"source_sha256": admission.source_hashes(root), "config": {"threshold": 1}})
    run(root, "add", "src", "research")
    run(root, "commit", "-m", "Register before scoring")
    commit = run(root, "rev-parse", "HEAD")
    return root, registration, commit


def test_unpushed_preregistration_is_not_permission(registered):
    root, registration, commit = registered
    with pytest.raises(ValueError, match="not a current pushed branch head"):
        admission.check_registration(root, registration, commit)
    run(root, "push", "origin", "topic")
    admission.check_registration(root, registration, commit)


def test_pushed_thresholds_cannot_change(registered):
    root, registration, commit = registered
    run(root, "push", "origin", "topic")
    freeze = json.loads(registration.read_text())
    freeze["config"]["threshold"] = 0
    admission.save(registration, freeze)
    with pytest.raises(ValueError, match="differs from pushed commit"):
        admission.check_registration(root, registration, commit)


def test_pushed_detector_cannot_change(registered):
    root, registration, commit = registered
    run(root, "push", "origin", "topic")
    (root / "src/evallab/scanner.py").write_text("threshold = 0\n")
    with pytest.raises(ValueError, match="Frozen code"):
        admission.check_registration(root, registration, commit)


def test_added_detector_cannot_escape_freeze(registered):
    root, registration, commit = registered
    run(root, "push", "origin", "topic")
    (root / "src/evallab/extra.py").write_text("threshold = 0\n")
    with pytest.raises(ValueError, match="inventory changed"):
        admission.check_registration(root, registration, commit)


def test_raw_intake_never_enters_git(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        admission.external(tmp_path / "raw")
