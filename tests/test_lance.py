"""Tests for embedders moved from evallab.lance to evallab.production.run_atlas_clusters."""

import json
import subprocess
import sys

from evallab.production.run_atlas_clusters import HashingEmbedder


def test_embedder_determinism_same_process():
    e = HashingEmbedder(dim=256)
    v1 = e.embed(["hello world test"])[0]
    v2 = e.embed(["hello world test"])[0]
    assert v1 == v2


def test_embedder_determinism_across_process(tmp_path):
    code = """
from evallab.production.run_atlas_clusters import HashingEmbedder
e = HashingEmbedder(256)
v = e.embed(["hello world test"])[0]
import json
print(json.dumps(v))
"""
    out1 = subprocess.check_output([sys.executable, "-c", code], text=True, cwd=tmp_path)
    out2 = subprocess.check_output([sys.executable, "-c", code], text=True, cwd=tmp_path)
    assert json.loads(out1) == json.loads(out2)


def test_embedder_dim_and_l2_norm():
    e = HashingEmbedder(256)
    v = e.embed(["some text here"])[0]
    assert len(v) == 256
    norm = sum(x * x for x in v)
    assert abs(norm - 1.0) < 1e-6
