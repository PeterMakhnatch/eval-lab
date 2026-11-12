"""Tests for embedders moved from evallab.lance to evallab.production.run_atlas_clusters."""

import json
import subprocess
import sys
from unittest.mock import patch

from evallab.production.run_atlas_clusters import HashingEmbedder, OpenAICompatibleEmbedder


def test_embedder_determinism_same_process():
    e = HashingEmbedder(dim=256)
    v1 = e.embed(["hello world test"])[0]
    v2 = e.embed(["hello world test"])[0]
    assert v1 == v2


def test_openai_compatible_embedder_pins_identity_and_normalizes():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "data": [
                        {"index": 0, "embedding": [3.0, 4.0]},
                        {"index": 1, "embedding": [0.0, 2.0]},
                    ]
                }
            ).encode()

    embedder = OpenAICompatibleEmbedder(
        endpoint="http://localhost:8000",
        model="sentence-transformer",
        model_revision="sha256-model-revision",
        tokenizer_id="sentence-transformer-tokenizer",
        tokenizer_revision="tokenizer-v1",
        dim=2,
        api_key="secret-not-in-digest",
    )
    with patch("urllib.request.urlopen", return_value=Response()):
        vectors = embedder.embed(["first", "second"])
    assert vectors == [[0.6, 0.8], [0.0, 1.0]]
    assert len(embedder.digest) == 64
    assert "secret-not-in-digest" not in repr(embedder)


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
