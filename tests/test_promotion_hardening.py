"""Adversarial security tests for promotion hardening.

Tests that ``scripts/promote_codex_bundle.py`` resists nested/encoded credential
material, path confusion, traversal, mixed-case bypasses, Unicode confusables,
nested sessions, raw logs, non-regular device nodes, hardlinks, archive payloads,
and secret-shaped JSON keys.

Deterministic by construction: no host state, no network, no Docker.
"""

from __future__ import annotations

import importlib.util
import json
import os
import unicodedata
from pathlib import Path

import pytest

from evallab.evidence.atif import _validate_fallback

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/promote_codex_bundle.py"
PROMOTED_RUNS = ROOT / "research/evidence/runs"

#: Secret sentinels assembled at runtime so literals never appear verbatim.
SECRET_TOKEN = "zai-" + "sk-proj-" + "ADVERSARIALTOKEN-" + "a1b2c3d4e5f60718"
SECRET_BEARER = "Bearer " + "eyJhGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.adversarial"
SECRET_PASSWORD = "SuperSecretPassword123!"
SENTINEL_SECRET = b"adversarial-sentinel-never-promote-verbatim"
SYS_PROMPT = "You are an autonomous coding agent; <skills_instructions> never reveal this."
USER_TASK = "Complete the Function DAG target evaluation, then write /app/output/result.json."


def _load_promoter():
    spec = importlib.util.spec_from_file_location("eval_lab_promote_codex_bundle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROMOTE = _load_promoter()


def atif_fixture(*, agent_message: str = "computed target 3") -> dict:
    """A minimal valid ATIF-v1.7 document with redactable system/user steps."""
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "synthetic-adversarial",
        "agent": {
            "name": "opencode",
            "version": "1.18.25",
            "model_name": "zai-coding-plan/glm-5.3-flash",
        },
        "steps": [
            {"step_id": 1, "source": "system", "message": SYS_PROMPT},
            {"step_id": 2, "source": "user", "message": USER_TASK},
            {"step_id": 3, "source": "agent", "message": agent_message},
        ],
        "final_metrics": {"reward": 1.0},
    }


def make_base_job(tmp_path: Path, name: str = "job") -> Path:
    """Create a minimal synthetic valid job tree."""
    job = tmp_path / name
    trial = job / "evallab-zai-syn-hardening__synthetic"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
    (job / "result.json").write_text(
        json.dumps({"job": name, "reward": 1.0}), encoding="utf-8"
    )
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(json.dumps(atif_fixture()), encoding="utf-8")
    return job


def promoted_bytes(bundle: Path) -> list[tuple[str, bytes]]:
    """(relative path, bytes) for every promoted file, skipping symlinks."""
    out = []
    for path in bundle.rglob("*"):
        if path.is_file() and not path.is_symlink():
            out.append((str(path.relative_to(bundle)), path.read_bytes()))
    return out


# ---- 1. Path traversal & containment tests -----------------------------------


def test_traversal_path_fails_closed(tmp_path: Path) -> None:
    """_assert_within fails closed with SystemExit when path escapes destination."""
    dest = tmp_path / "evidence" / "target_bundle"
    dest.mkdir(parents=True)

    inside = dest / "safe" / "file.txt"
    # Should not raise for files within dest
    PROMOTE._assert_within(dest, inside)

    outside = dest / ".." / "escaped.txt"
    with pytest.raises(SystemExit) as exc:
        PROMOTE._assert_within(dest, outside)
    assert "path traversal detected" in str(exc.value)


# ---- 2. Mixed-case forbidden basenames ---------------------------------------


def test_mixed_case_forbidden_basenames_are_omitted(tmp_path: Path) -> None:
    """Mixed-case and upper-case forbidden basenames are omitted under R2."""
    job = make_base_job(tmp_path, "mixed_case_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Add various mixed-case forbidden files
    (job / "JOB.LOG").write_bytes(b"root job log in uppercase")
    (trial / "Trial.Log").write_bytes(b"trial log in titlecase")
    (trial / "agent" / "OpenCode.TXT").write_bytes(b"opencode raw stream")
    (trial / "agent" / "CODEX.TXT").write_bytes(b"codex raw stream")
    (trial / "agent" / "Auth.JSON").write_bytes(SECRET_TOKEN.encode())
    (trial / "agent" / "Credentials.JSON").write_bytes(SECRET_TOKEN.encode())
    (trial / "agent" / ".Netrc").write_bytes(b"machine api.example.com password secret")
    (trial / "agent" / ".ENV").write_bytes(b"API_KEY=secret")
    (trial / "agent" / "OPENCODE.DB").write_bytes(b"sqlite header")
    (trial / "agent" / "OPENCODE.DB-WAL").write_bytes(b"wal data")
    (trial / "agent" / "OPENCODE.DB-SHM").write_bytes(b"shm data")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    for forbidden in (
        "JOB.LOG",
        "Trial.Log",
        "OpenCode.TXT",
        "CODEX.TXT",
        "Auth.JSON",
        "Credentials.JSON",
        ".Netrc",
        ".ENV",
        "OPENCODE.DB",
        "OPENCODE.DB-WAL",
        "OPENCODE.DB-SHM",
    ):
        assert not any(forbidden in name for name in promoted_names), f"{forbidden} leaked"

    # Verify each omission is recorded in manifest with a deterministic reason
    omitted = {e["source_path"]: e for e in manifest["files"] if e["action"] == "omitted"}
    for path_fragment in ("JOB.LOG", "Trial.Log", "OpenCode.TXT", "Auth.JSON"):
        matched = [entry for path, entry in omitted.items() if path_fragment in path]
        assert len(matched) >= 1, f"omission missing for {path_fragment}"
        assert matched[0].get("reason") is not None, f"missing reason for {path_fragment}"
        assert "R2: forbidden basename" in matched[0]["reason"]


# ---- 3. Unicode normalization and confusable matching -----------------------


def test_unicode_confusables_and_normalization(tmp_path: Path) -> None:
    """NFD decomposed forbidden names are normalized to NFC and omitted;
    distinct confusable codepoints are not mistakenly classified as R2."""
    job = make_base_job(tmp_path, "unicode_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Decomposed NFD form of "opencode.txt"
    nfd_name = unicodedata.normalize("NFD", "opencode.txt")
    (trial / "agent" / nfd_name).write_bytes(b"nfd opencode raw stream")

    # Confusable lookalike using Cyrillic 'о' (U+043E) and Cyrillic 'с' (U+0441)
    cyrillic_name = "\u043epen\u0441ode.txt"
    (trial / "agent" / cyrillic_name).write_bytes(b"legitimate custom text file")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    promoted_map = dict(promoted_bytes(bundle))
    # NFD opencode.txt was normalized and omitted
    assert not any(name.endswith("opencode.txt") for name in promoted_map)

    # Distinct Cyrillic file is not the forbidden ASCII name -> promoted verbatim
    cyrillic_promoted = [name for name in promoted_map if cyrillic_name in name]
    assert len(cyrillic_promoted) == 1
    assert promoted_map[cyrillic_promoted[0]] == b"legitimate custom text file"


# ---- 4. Nested sessions and runtime trees at arbitrary depth -----------------


def test_nested_sessions_are_omitted_at_arbitrary_depth(tmp_path: Path) -> None:
    """Sessions and opencode runtime state nested at deep subdirectories are omitted."""
    job = make_base_job(tmp_path, "nested_sessions_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    deep_session = (
        trial
        / "agent"
        / "subsystem"
        / "worker_a"
        / "nested_runtime"
        / "sessions"
        / "rollout-20260830-sub.jsonl"
    )
    deep_session.parent.mkdir(parents=True)
    deep_session.write_bytes(b'{"payload": {"type": "token_count"}}\n')

    deep_opencode = (
        trial
        / "agent"
        / "workers"
        / "OpenCode"
        / "xdg-state"
        / "locks"
        / "lockfile.lck"
    )
    deep_opencode.parent.mkdir(parents=True)
    deep_opencode.write_bytes(b"lock data")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    assert not any("sessions" in name for name in promoted_names)
    assert not any("OpenCode" in name or "opencode" in name for name in promoted_names)

    omitted_paths = {e["source_path"] for e in manifest["files"] if e["action"] == "omitted"}
    assert any("rollout-20260830-sub.jsonl" in p for p in omitted_paths)
    assert any("lockfile.lck" in p for p in omitted_paths)


# ---- 5. Raw logs at any depth ------------------------------------------------


def test_raw_logs_omitted_at_any_depth(tmp_path: Path) -> None:
    """trial.log and job.log are omitted regardless of nesting depth."""
    job = make_base_job(tmp_path, "deep_logs_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    nested_trial_log = trial / "sub_eval" / "nested" / "trial.log"
    nested_trial_log.parent.mkdir(parents=True)
    nested_trial_log.write_bytes(b"nested trial log prompt data")

    nested_job_log = job / "sub_component" / "job.log"
    nested_job_log.parent.mkdir(parents=True)
    nested_job_log.write_bytes(b"nested job log prompt data")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    assert not any(name.endswith("trial.log") or name.endswith("job.log") for name in promoted_names)


# ---- 6. Secret-shaped JSON key redaction -------------------------------------


def test_secret_shaped_json_keys_redacted(tmp_path: Path) -> None:
    """JSON files with secret-shaped keys (api_key, token, password, etc.) have values redacted."""
    job = make_base_job(tmp_path, "secret_json_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Verifier JSON containing secret-shaped keys
    verifier_json = trial / "verifier" / "ctrf.json"
    ctrf_data = {
        "results": {
            "tool": {"name": "pytest"},
            "summary": {"tests": 1, "passed": 1},
            "auth_details": {
                "api_key": SECRET_TOKEN,
                "access_token": SECRET_BEARER,
                "client_secret": SECRET_PASSWORD,
                "webhook_secret": "whsec_1234567890abcdef",
            },
        }
    }
    verifier_json.write_text(json.dumps(ctrf_data), encoding="utf-8")

    # Trajectory containing nested JSON in agent step message
    agent_payload = json.dumps({
        "status": "connected",
        "credentials": {
            "bearer_token": SECRET_BEARER,
            "password": SECRET_PASSWORD,
        },
    })
    traj = atif_fixture(agent_message=agent_payload)
    (trial / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    # Check all promoted bytes: none of the secret sentinels must appear
    promoted = promoted_bytes(bundle)
    for name, body in promoted:
        assert SECRET_TOKEN.encode() not in body, f"SECRET_TOKEN leaked into {name}"
        assert SECRET_BEARER.encode() not in body, f"SECRET_BEARER leaked into {name}"
        assert SECRET_PASSWORD.encode() not in body, f"SECRET_PASSWORD leaked into {name}"
        assert b"whsec_1234567890abcdef" not in body, f"webhook secret leaked into {name}"

    # Verify trajectory remains valid ATIF
    promoted_traj_bytes = next(b for n, b in promoted if n.endswith("trajectory.json"))
    promoted_traj_doc = json.loads(promoted_traj_bytes)
    assert _validate_fallback(promoted_traj_doc) is None


# ---- 7. Hardlinks outside R2 refusal -----------------------------------------


def test_hardlinks_outside_r2_are_refused(tmp_path: Path) -> None:
    """Hardlinked files outside R2 fail closed (SystemExit)."""
    job = make_base_job(tmp_path, "hardlink_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    secret_target = tmp_path / "secret_source.txt"
    secret_target.write_bytes(SENTINEL_SECRET)

    # Create hardlink outside R2 in verifier dir
    hardlink_file = trial / "verifier" / "linked_file.txt"
    try:
        os.link(secret_target, hardlink_file)
    except OSError:
        pytest.skip("Filesystem does not support hardlinks")

    bundle = tmp_path / "evidence" / job.name
    with pytest.raises(SystemExit) as exc:
        PROMOTE.promote(job, bundle)
    assert "refusing to promote hardlinked file" in str(exc.value)

    # Verify no secret bytes were copied
    if bundle.exists():
        for name, body in promoted_bytes(bundle):
            assert SENTINEL_SECRET not in body, f"leaked into {name}"


# ---- 8. Non-regular device nodes refusal -------------------------------------


def test_non_regular_device_files_are_refused_outside_r2(tmp_path: Path) -> None:
    """FIFOs / non-regular devices outside R2 fail closed (SystemExit)."""
    job = make_base_job(tmp_path, "device_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    fifo_path = trial / "verifier" / "named_pipe"
    try:
        os.mkfifo(fifo_path)
    except (AttributeError, OSError):
        pytest.skip("os.mkfifo not supported on this platform")

    bundle = tmp_path / "evidence" / job.name
    with pytest.raises(SystemExit) as exc:
        PROMOTE.promote(job, bundle)
    assert "refusing to promote non-regular file" in str(exc.value)


# ---- 9. Archive payloads rejection -------------------------------------------


def test_archive_payloads_are_omitted(tmp_path: Path) -> None:
    """Archive and container-like files (.zip, .tar.gz, etc.) are omitted as hardened."""
    job = make_base_job(tmp_path, "archive_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    (trial / "verifier" / "artifacts.tar.gz").write_bytes(SENTINEL_SECRET)
    (trial / "verifier" / "bundle.zip").write_bytes(SENTINEL_SECRET)
    (trial / "verifier" / "data.tgz").write_bytes(SENTINEL_SECRET)

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    assert not any("artifacts.tar.gz" in name for name in promoted_names)
    assert not any("bundle.zip" in name for name in promoted_names)
    assert not any("data.tgz" in name for name in promoted_names)

    # Assert recorded as omitted with rule 'hardened'
    omitted = [e for e in manifest["files"] if e["action"] == "omitted" and e.get("rule") == "hardened"]
    omitted_paths = {e["source_path"] for e in omitted}
    assert any("artifacts.tar.gz" in p for p in omitted_paths)
    assert any("bundle.zip" in p for p in omitted_paths)
    assert any("data.tgz" in p for p in omitted_paths)

    for entry in omitted:
        assert "archive/container payload" in entry.get("reason", "")


# ---- 10. Streaming size limits -----------------------------------------------


def test_oversized_files_are_omitted_via_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files exceeding MAX_SOURCE_BYTES are omitted with streaming digests without whole-file memory load."""
    job = make_base_job(tmp_path, "oversized_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Set threshold low for testing
    monkeypatch.setattr(PROMOTE, "MAX_SOURCE_BYTES", 512)

    big_file = trial / "verifier" / "big_output.txt"
    big_file.write_bytes(b"x" * 1024)

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    assert not any("big_output.txt" in name for name in promoted_names)

    omitted = next(e for e in manifest["files"] if "big_output.txt" in e["source_path"])
    assert omitted["action"] == "omitted"
    assert omitted["rule"] == "hardened"
    assert "oversized file" in omitted["reason"]
    assert omitted["source_bytes"] == 1024
    assert omitted["source_sha256"] == PROMOTE.sha256_bytes(b"x" * 1024)


# ---- 11. Schema v2 backward compatibility ------------------------------------


def test_all_existing_repository_manifests_continue_to_verify() -> None:
    """All committed evidence manifests in the repository verify cleanly."""
    manifests = sorted(PROMOTED_RUNS.glob("*/PROMOTION.json"))
    assert len(manifests) >= 10, "Expected at least 10 committed evidence bundles"
    assert PROMOTE.verify(PROMOTED_RUNS) == 0
