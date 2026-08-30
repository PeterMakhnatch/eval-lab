"""Adversarial security tests for promotion hardening.

Tests that ``scripts/promote_codex_bundle.py`` resists nested/encoded credential
material, path confusion, traversal, mixed-case bypasses, Unicode confusables,
nested sessions, raw logs, non-regular device nodes, hardlinks, archive payloads,
secret-shaped JSON keys, ATIF content-part lists, uppercase verifier JSON suffixes,
oversized streaming rollouts with bounded dropped names, manifest traversal in verify(),
and CLI path injection / root-deletion attempts via --job.

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


def atif_fixture(*, agent_message: str | list = "computed target 3") -> dict:
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
    PROMOTE.promote(job, bundle)

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
    PROMOTE.promote(job, bundle)

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
                "access_tokens": [SECRET_TOKEN],
                "refresh_tokens": [SECRET_TOKEN],
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


# ---- 12. Negative CLI & job basename injection / root-deletion tests ---------


def test_cli_job_dot_cannot_delete_evidence_root(tmp_path: Path) -> None:
    """--job . with --force must fail closed and never delete the evidence root."""
    runs = tmp_path / "runs"
    runs.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    evidence_sentinel = evidence / "DO_NOT_DELETE.txt"
    evidence_sentinel.write_bytes(SENTINEL_SECRET)
    runs_sentinel = runs / "RUNS_ROOT_SENTINEL.txt"
    runs_sentinel.write_bytes(SENTINEL_SECRET)

    with pytest.raises(SystemExit):
        PROMOTE.main([
            "--source-runs", str(runs),
            "--evidence-runs", str(evidence),
            "--job", ".",
            "--force",
        ])

    assert evidence.is_dir(), "evidence root must survive"
    assert evidence_sentinel.exists(), "evidence root sentinel must not be deleted"
    assert runs_sentinel.exists(), "runs root sentinel must not be deleted"


def test_cli_job_dotdot_cannot_delete_or_escape(tmp_path: Path) -> None:
    """--job .. with --force must fail closed."""
    runs = tmp_path / "runs"
    runs.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    sentinel = evidence / "EVIDENCE_SENTINEL.txt"
    sentinel.write_bytes(SENTINEL_SECRET)

    with pytest.raises(SystemExit):
        PROMOTE.main([
            "--source-runs", str(runs),
            "--evidence-runs", str(evidence),
            "--job", "..",
            "--force",
        ])

    assert sentinel.exists()


def test_cli_job_absolute_path_is_rejected(tmp_path: Path) -> None:
    """--job /absolute/path must fail closed."""
    runs = tmp_path / "runs"
    runs.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    sentinel = evidence / "EVIDENCE_SENTINEL.txt"
    sentinel.write_bytes(SENTINEL_SECRET)

    with pytest.raises(SystemExit):
        PROMOTE.main([
            "--source-runs", str(runs),
            "--evidence-runs", str(evidence),
            "--job", "/etc/passwd",
            "--force",
        ])

    assert sentinel.exists()


def test_cli_job_path_separators_are_rejected(tmp_path: Path) -> None:
    """--job with path separators (traversal or subdirectories) fails closed."""
    runs = tmp_path / "runs"
    runs.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    sentinel = evidence / "EVIDENCE_SENTINEL.txt"
    sentinel.write_bytes(SENTINEL_SECRET)

    for invalid_job in ("../escaped", "subdir/job1", "..\\escaped", "a/b/c"):
        with pytest.raises(SystemExit):
            PROMOTE.main([
                "--source-runs", str(runs),
                "--evidence-runs", str(evidence),
                "--job", invalid_job,
                "--force",
            ])
        assert sentinel.exists(), f"sentinel deleted by {invalid_job}"


def test_promote_function_rejects_unsafe_destination_or_source(tmp_path: Path) -> None:
    """Direct calls to promote() with unsafe paths fail before any rmtree or existence check."""
    valid_job = make_base_job(tmp_path, "valid_job")
    valid_dest = tmp_path / "evidence" / "valid_bundle"

    # Unsafe destination names
    for unsafe_dest in (Path("."), Path(".."), Path("/"), Path("")):
        with pytest.raises(SystemExit):
            PROMOTE.promote(valid_job, unsafe_dest, force=True)

    # Unsafe source names
    for unsafe_source in (Path("."), Path(".."), Path("/"), Path("")):
        with pytest.raises(SystemExit):
            PROMOTE.promote(unsafe_source, valid_dest, force=True)


# ---- 13. Additional Security Review Defect Tests ----------------------------


def test_verify_detects_traversal_in_manifest_promoted_path(tmp_path: Path) -> None:
    """verify() checks promoted_path containment against bundle and fails closed on traversal."""
    job = make_base_job(tmp_path, "traversal_manifest_job")
    evidence = tmp_path / "evidence"
    bundle = evidence / job.name
    PROMOTE.promote(job, bundle)

    manifest_path = bundle / "PROMOTION.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Forge a traversal promoted_path in manifest
    manifest["files"].append({
        "source_path": "evil.txt",
        "promoted_path": "../escaped_secret.txt",
        "action": "verbatim",
        "rule": None,
        "source_bytes": 10,
        "source_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
        "promoted_bytes": 10,
        "promoted_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    })
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # verify() must flag failure and not crash or disclose
    assert PROMOTE.verify(evidence) != 0, "verify must fail on manifest promoted_path traversal"


def test_root_result_json_streaming_sha256(tmp_path: Path) -> None:
    """Root result.json digest in manifest is computed via streaming without unbounded memory."""
    job = make_base_job(tmp_path, "result_json_job")
    result_file = job / "result.json"
    data = json.dumps({"job": "result_json_job", "status": "completed", "scores": [1.0] * 50})
    result_file.write_text(data, encoding="utf-8")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    expected_sha256 = PROMOTE.sha256_bytes(data.encode("utf-8"))
    assert manifest["source_job_result_sha256"] == expected_sha256


def test_atif_content_parts_and_whitespace_json_redaction(tmp_path: Path) -> None:
    """ATIF content parts in system/user steps and leading-whitespace JSON in agent steps are redacted."""
    job = make_base_job(tmp_path, "atif_content_parts_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Trajectory with content-part list in user step and leading whitespace JSON in agent step
    traj = {
        "schema_version": "ATIF-v1.7",
        "session_id": "synthetic-content-parts",
        "agent": {
            "name": "opencode",
            "version": "1.18.25",
            "model_name": "zai-coding-plan/glm-5.3-flash",
        },
        "steps": [
            {
                "step_id": 1,
                "source": "system",
                "message": [{"type": "text", "text": SYS_PROMPT}],
            },
            {
                "step_id": 2,
                "source": "user",
                "message": [{"type": "text", "text": USER_TASK}],
            },
            {
                "step_id": 3,
                "source": "agent",
                "message": f"   \n\t{{\"refreshToken\": \"{SECRET_TOKEN}\", \"status\": \"ok\"}}",
            },
        ],
        "final_metrics": {"reward": 1.0},
    }
    (trial / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    promoted = promoted_bytes(bundle)
    for name, body in promoted:
        assert SYS_PROMPT.encode() not in body, f"SYS_PROMPT leaked into {name}"
        assert USER_TASK.encode() not in body, f"USER_TASK leaked into {name}"
        assert SECRET_TOKEN.encode() not in body, f"SECRET_TOKEN leaked into {name}"

    traj_bytes = next(b for n, b in promoted if n.endswith("trajectory.json"))
    doc = json.loads(traj_bytes)
    assert _validate_fallback(doc) is None


def test_verifier_uppercase_json_suffix_redaction(tmp_path: Path) -> None:
    """Verifier files with .JSON or .Json suffix are parsed and redacted as JSON."""
    job = make_base_job(tmp_path, "uppercase_json_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Verifier JSON with uppercase extension
    (trial / "verifier" / "REPORT.JSON").write_text(
        json.dumps({"githubToken": SECRET_TOKEN, "score": 1.0}), encoding="utf-8"
    )

    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    for name, body in promoted_bytes(bundle):
        assert SECRET_TOKEN.encode() not in body, f"secret leaked in {name}"


def test_expanded_secret_keys_redaction(tmp_path: Path) -> None:
    """camelCase, snake_case, and expanded secret-shaped keys are all redacted."""
    job = make_base_job(tmp_path, "expanded_secrets_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    verifier_payload = {
        "credentials": {
            "secretKey": SECRET_TOKEN,
            "refreshToken": SECRET_TOKEN,
            "refresh_tokens": [SECRET_TOKEN],
            "access_tokens": [SECRET_TOKEN],
            "githubToken": SECRET_TOKEN,
            "passphrase": SECRET_PASSWORD,
            "jwtToken": "eyJhbGciOi...",
            "authSecret": "authsec_999",
            "apiKey": "ak_12345",
            "bearerToken": "bt_67890",
        }
    }
    (trial / "verifier" / "secrets.json").write_text(
        json.dumps(verifier_payload), encoding="utf-8"
    )

    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    for name, body in promoted_bytes(bundle):
        assert SECRET_TOKEN.encode() not in body, f"leaked in {name}"
        assert SECRET_PASSWORD.encode() not in body, f"leaked in {name}"
        assert b"eyJhbGciOi..." not in body, f"jwt leaked in {name}"
        assert b"authsec_999" not in body, f"authSecret leaked in {name}"


def test_nested_secret_context_propagation(tmp_path: Path) -> None:
    """Secret parent context propagates through nested dicts/lists to redact leaf scalar values."""
    job = make_base_job(tmp_path, "nested_secret_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    payload = {
        "credentials": {
            "custom_endpoint": "https://auth.internal.corp",
            "nested_value": SECRET_TOKEN,
            "servers": ["server1.auth", SECRET_PASSWORD],
        },
        "auth": [
            {"username": "admin", "secret_payload": SECRET_TOKEN}
        ],
    }
    (trial / "verifier" / "nested_auth.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    for name, body in promoted_bytes(bundle):
        assert SECRET_TOKEN.encode() not in body, f"secret leaked in {name}"
        assert SECRET_PASSWORD.encode() not in body, f"password leaked in {name}"
        assert b"https://auth.internal.corp" not in body, f"nested url leaked in {name}"
        assert b"server1.auth" not in body, f"nested list item leaked in {name}"


def test_benign_metrics_and_indicators_preservation(tmp_path: Path) -> None:
    """Benign metric keys and indicator keys are preserved verbatim and not falsely redacted."""
    job = make_base_job(tmp_path, "metrics_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    metrics_payload = {
        "summary": {
            "token_count": 12345,
            "input_tokens": 8000,
            "output_tokens": 4345,
            "total_tokens": 12345,
            "prompt_tokens": 7500,
            "completion_tokens": 4000,
            "cached_tokens": 500,
            "reasoning_tokens": 345,
            "tokens_per_second": 42.5,
            "api_key_present": True,
            "credentials_count": 2,
            "auth_status": "verified",
            "has_api_key": True,
            "is_authenticated": True,
            "auth_type": "bearer",
            "auth_method": "oauth2",
            "auth_state": "active",
        }
    }
    (trial / "verifier" / "metrics.json").write_text(
        json.dumps(metrics_payload), encoding="utf-8"
    )

    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    promoted_metrics = next(
        b for n, b in promoted_bytes(bundle) if n.endswith("metrics.json")
    )
    doc = json.loads(promoted_metrics)
    assert doc["summary"]["token_count"] == 12345
    assert doc["summary"]["input_tokens"] == 8000
    assert doc["summary"]["total_tokens"] == 12345
    assert doc["summary"]["tokens_per_second"] == 42.5
    assert doc["summary"]["api_key_present"] is True
    assert doc["summary"]["credentials_count"] == 2
    assert doc["summary"]["auth_status"] == "verified"
    assert doc["summary"]["has_api_key"] is True
    assert doc["summary"]["is_authenticated"] is True
    assert doc["summary"]["auth_type"] == "bearer"


def test_mixed_case_agent_sessions_quota_sidecar(tmp_path: Path) -> None:
    """Mixed-case Agent/Sessions paths extract quota sidecars without crashing."""
    job = make_base_job(tmp_path, "mixed_case_quota_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    rollout_line = json.dumps({
        "timestamp": "2026-08-30T01:00:00Z",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "mixed_case_limit",
                "primary": {"used_percent": 15.0, "window_minutes": 60, "resets_at": 500},
            },
        },
    })
    # Mixed case directory Agent/Sessions
    agent_dir = trial / "Agent"
    sessions_dir = agent_dir / "Sessions"
    sessions_dir.mkdir(parents=True)
    rollout_file = sessions_dir / "rollout-20260830-mixed.jsonl"
    rollout_file.write_text(f"{rollout_line}\n", encoding="utf-8")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    # Rollout is omitted
    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    assert not any("rollout-20260830-mixed.jsonl" in name for name in promoted_names)

    # Sidecar is extracted under Agent/quota
    sidecars = [e for e in manifest["files"] if e.get("rule") == "R4"]
    assert len(sidecars) == 1
    assert "Agent" in sidecars[0]["promoted_path"] or "agent" in sidecars[0]["promoted_path"]
    assert sidecars[0]["promoted_path"].endswith(".rate-limits.json")


def test_oversized_session_rollout_extracts_quota_sidecar_via_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversized R2 rollout extracts R4 quota sidecar by streaming line-by-line without loading whole file."""
    job = make_base_job(tmp_path, "oversized_rollout_job")
    trial = job / "evallab-zai-syn-hardening__synthetic"

    # Set threshold low for testing streaming path
    monkeypatch.setattr(PROMOTE, "MAX_SOURCE_BYTES", 256)

    # Rollout with quota snapshot and many dropped fields to test bounded dropped names
    rate_limits = {
        "limit_id": "test_limit",
        "primary": {"used_percent": 42.0, "window_minutes": 60, "resets_at": 1000},
    }
    # Add 250 unrecognized fields
    for i in range(250):
        rate_limits[f"extra_unrecognised_dropped_field_number_{i}"] = f"value_{i}"

    rollout_line = json.dumps({
        "timestamp": "2026-08-30T00:00:00Z",
        "payload": {
            "type": "token_count",
            "rate_limits": rate_limits,
        },
    })
    # Padding line to exceed MAX_SOURCE_BYTES (256 bytes)
    padding_line = json.dumps({"type": "noise", "data": "a" * 300})

    sessions = trial / "agent" / "sessions"
    sessions.mkdir(parents=True)
    rollout_path = sessions / "rollout-20260830-test.jsonl"
    rollout_path.write_text(f"{rollout_line}\n{padding_line}\n", encoding="utf-8")

    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    # Rollout is omitted under R2
    promoted_names = [name for name, _ in promoted_bytes(bundle)]
    assert not any("rollout-20260830-test.jsonl" in name for name in promoted_names)

    # Quota sidecar is created, bounded, and valid
    sidecars = [e for e in manifest["files"] if e.get("rule") == "R4"]
    assert len(sidecars) == 1
    assert sidecars[0]["promoted_path"].endswith(".rate-limits.json")

    sidecar_file = bundle / sidecars[0]["promoted_path"]
    sidecar_data = json.loads(sidecar_file.read_text(encoding="utf-8"))
    assert sidecar_data["snapshot_count"] == 1
    assert sidecar_data["snapshots"][0]["rate_limits"]["limit_id"] == "test_limit"
    # Verify dropped field names are capped and overflow recorded
    assert len(sidecar_data["dropped_field_names"]) <= PROMOTE.MAX_DROPPED_NAMES
    assert sidecar_data.get("dropped_field_overflow_count", 0) > 0
    assert len(sidecar_file.read_bytes()) <= PROMOTE.MAX_SIDECAR_BYTES
