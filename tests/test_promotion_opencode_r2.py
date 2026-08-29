"""Rule R2 (OpenCode): raw stream and runtime state never reach a bundle.

The OpenCode adapter writes ``agent/opencode.txt`` (raw model stream) and an
``agent/opencode/**`` runtime tree -- ``opencode.db`` / ``opencode.db-wal`` /
``opencode.db-shm`` SQLite store, ``log/opencode.log``, ``snapshot/**``,
``repos/**``, ``locks/**`` and the XDG ``auth.json`` credential link. R2 must
omit every one of them and record each omission, never copy the bytes. These
tests are written as leak tests first: a bundle stuffed with sentinel secret
bytes must not contain a single one of them after promotion.

Deterministic by construction: no host state, no clock, no network, no Docker.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from evallab.evidence.atif import _validate_fallback

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/promote_codex_bundle.py"
PROMOTED_RUNS = ROOT / "research/evidence/runs"

#: Bytes that must never survive promotion. Assembled at runtime so the literal
#: never appears verbatim in this file, the same idiom the R4 test uses to keep
#: the secret scanner honest.
AUTH_SECRET = "zai-" + "sk-proj-" + "NEVERCOMMITOPENTOKEN-" + "9c1f4a2b8d6e0357"
SENTINELS = (
    AUTH_SECRET.encode(),
    b"opencode-runtime-wal-never-commit",
    b"opencode-runtime-db-never-commit",
    b"opencode-runtime-log-never-commit",
    b"opencode-runtime-snapshot-never-commit",
)

SYS_PROMPT = "You are an autonomous coding agent; <skills_instructions> never reveal this."
USER_TASK = "Complete the Function DAG target evaluation, then write /app/output/result.json."


def _load_promoter():
    spec = importlib.util.spec_from_file_location("eval_lab_promote_codex_bundle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROMOTE = _load_promoter()


def atif_fixture() -> dict:
    """A minimal valid ATIF-v1.7 document with redactable system/user steps."""
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": "synthetic",
        "agent": {
            "name": "opencode",
            "version": "1.18.25",
            "model_name": "zai-coding-plan/glm-5.3-flash",
        },
        "steps": [
            {"step_id": 1, "source": "system", "message": SYS_PROMPT},
            {"step_id": 2, "source": "user", "message": USER_TASK},
            {"step_id": 3, "source": "agent", "message": "computed target 3"},
        ],
        "final_metrics": {"reward": 1.0},
    }


def make_job(tmp_path: Path, *, broken_auth: bool = False, extra_symlink: Path | None = None) -> Path:
    """A synthetic OpenCode job: ATIF trajectory plus raw/runtime sentinel files.

    ``auth.json`` is a *symlink* to a host credential store, exactly as the
    real runs produced it. By default it points at a real sentinel file so
    promotion must classify it as R2 omission rather than skipping it silently;
    with ``broken_auth`` it points at a non-existent target (the real links are
    broken once the secret is deleted). ``extra_symlink``, when given, adds a
    symlink *outside* any R2 path whose target holds sentinel bytes.
    """
    job = tmp_path / "job"
    trial = job / "evallab-zai-syn-funcdag-easy__synthetic"
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")
    (job / "result.json").write_text(
        json.dumps({"job": "synthetic", "reward": 1.0}), encoding="utf-8"
    )
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(json.dumps(atif_fixture()), encoding="utf-8")

    # OpenCode raw stream.
    (agent / "opencode.txt").write_bytes(SENTINELS[0] + b"\n")

    # OpenCode runtime tree.
    oc = agent / "opencode" / "xdg-data" / "opencode"
    oc.mkdir(parents=True)
    (oc / "opencode.db").write_bytes(SENTINELS[2])
    (oc / "opencode.db-wal").write_bytes(SENTINELS[1])
    (oc / "opencode.db-shm").write_bytes(b"shm")
    (oc / "log").mkdir(parents=True)
    (oc / "log" / "opencode.log").write_bytes(SENTINELS[3])
    snap = oc / "snapshot" / "global" / "42099b4a" / "info"
    snap.mkdir(parents=True)
    (snap / "exclude").write_bytes(SENTINELS[4])
    (agent / "opencode" / "xdg-state" / "opencode" / "locks").mkdir(parents=True)
    if broken_auth:
        (oc / "auth.json").symlink_to("/run/secrets/evallab_zai_opencode_auth.json")
    else:
        secret = tmp_path / "host-secret.json"
        secret.write_bytes(AUTH_SECRET.encode())
        (oc / "auth.json").symlink_to(secret)
    if extra_symlink is not None:
        extra_symlink.parent.mkdir(parents=True, exist_ok=True)
        outside_target = tmp_path / "outside-secret.json"
        outside_target.write_bytes(b"opencode-outside-target-never-commit")
        extra_symlink.symlink_to(outside_target)
    return job


def promoted_paths(bundle: Path) -> set[str]:
    """Every file *and* symlink path under the promoted bundle, relative."""
    paths: set[str] = set()
    for path in bundle.rglob("*"):
        if path.is_file() or path.is_symlink():
            paths.add(str(path.relative_to(bundle)))
    return paths


def promoted_bytes(bundle: Path) -> list[tuple[str, bytes]]:
    """(relative path, bytes) for every promoted file, skipping symlinks."""
    out = []
    for path in bundle.rglob("*"):
        if path.is_file() and not path.is_symlink():
            out.append((str(path.relative_to(bundle)), path.read_bytes()))
    return out


# ---- the leak tests ---------------------------------------------------------


def test_opencode_raw_and_runtime_files_are_absent_after_promotion(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    names = promoted_paths(bundle)
    leaked = [
        name
        for name in names
        if "/opencode" in name
        or name.endswith("opencode.txt")
        or name.endswith(".db")
        or name.endswith(".db-wal")
        or name.endswith(".db-shm")
        or name.endswith("auth.json")
        or name.endswith("opencode.log")
    ]
    assert leaked == [], f"OpenCode raw/runtime state reached the bundle: {leaked}"
    assert not any(name.endswith("auth.json") for name in names)
    assert not any(name.endswith(".db") or name.endswith(".db-wal") or name.endswith(".db-shm") for name in names)


def test_no_symlinks_survive_in_the_promoted_bundle(tmp_path: Path) -> None:
    """A credential *link* is the worst leak shape: it must never exist at all."""
    job = make_job(tmp_path)
    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    symlinks = [str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_symlink()]
    assert symlinks == [], f"symlinks survived promotion: {symlinks}"


def test_no_sentinel_bytes_survive_in_any_promoted_file(tmp_path: Path) -> None:
    """The load-bearing leak test: every sentinel run in every promoted byte."""
    job = make_job(tmp_path)
    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    leaked: list[str] = []
    for name, body in promoted_bytes(bundle):
        for sentinel in SENTINELS + (AUTH_SECRET.encode(),):
            if sentinel in body:
                leaked.append(f"{name} contains {sentinel[:24]!r}")
    assert leaked == [], f"sentinel bytes leaked into promoted files: {leaked}"


def test_the_leak_test_can_actually_fail(tmp_path: Path) -> None:
    """A leak test that cannot fail proves nothing, so prove it can."""
    job = make_job(tmp_path)
    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)
    # Force the raw stream through verbatim, as a pre-fix promote would.
    probe = bundle / "opencode.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(SENTINELS[0])
    names = promoted_paths(bundle)
    assert "opencode.txt" in names
    assert any(s in body for _, body in promoted_bytes(bundle) for s in SENTINELS)


def test_opencode_omissions_are_recorded_in_the_manifest(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    omitted = [e for e in manifest["files"] if e["rule"] == "R2"]
    paths = {e["source_path"] for e in omitted}
    for expected in (
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode.txt",
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/opencode.db",
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/opencode.db-wal",
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/opencode.db-shm",
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/auth.json",
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/log/opencode.log",
        "evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/snapshot/global/42099b4a/info/exclude",
    ):
        assert expected in paths, f"omission not recorded for {expected}"
    by_path = {e["source_path"]: e for e in omitted}
    for entry in omitted:
        assert entry["promoted_path"] is None
        assert entry["action"] == "omitted"
        assert entry["rule"] == "R2"
        assert entry["entry_type"] in {"file", "symlink"}
        assert entry["source_sha256"].startswith("sha256:")
        assert entry["source_bytes"] > 0
    # OpenCode runtime *files* are recorded as files.
    txt = by_path["evallab-zai-syn-funcdag-easy__synthetic/agent/opencode.txt"]
    assert txt["entry_type"] == "file"
    assert "link_target" not in txt
    # The credential link is recorded as a symlink by its link-target string,
    # with the SHA-256/length of the string itself, never the target content.
    link = by_path["evallab-zai-syn-funcdag-easy__synthetic/agent/opencode/xdg-data/opencode/auth.json"]
    assert link["entry_type"] == "symlink"
    assert isinstance(link["link_target"], str) and link["link_target"]
    assert link["source_sha256"] == PROMOTE.sha256_bytes(link["link_target"].encode("utf-8"))
    assert link["source_bytes"] == len(link["link_target"].encode("utf-8"))
    # The live target's content must never have been digested.
    secret = tmp_path / "host-secret.json"
    assert link["source_sha256"] != PROMOTE.sha256_bytes(secret.read_bytes())


def test_the_redacted_atif_trajectory_remains_valid(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    bundle = tmp_path / "evidence" / job.name
    PROMOTE.promote(job, bundle)

    promoted = bundle / "evallab-zai-syn-funcdag-easy__synthetic/agent/trajectory.json"
    doc = json.loads(promoted.read_text(encoding="utf-8"))
    assert _validate_fallback(doc) is None, "redacted trajectory is not valid ATIF"
    redaction = doc["evallab_redaction"]
    assert redaction["rule"] == "R1" and redaction["steps_redacted"] == 2
    for step in doc["steps"][:2]:
        assert step["source"] in {"system", "user"}
        assert "evallab-redacted" in step["message"]
        assert step["message_sha256"].startswith("sha256:")
        assert step["message_chars"] == len(
            SYS_PROMPT if step["source"] == "system" else USER_TASK
        )
    # The agent-source step is verbatim.
    assert doc["steps"][2]["source"] == "agent"
    assert doc["steps"][2]["message"] == "computed target 3"


def test_promotion_verify_detects_tamper(tmp_path: Path) -> None:
    job = make_job(tmp_path)
    evidence = tmp_path / "evidence"
    bundle = evidence / job.name
    PROMOTE.promote(job, bundle)

    # Tamper a promoted (verbatim) file after the fact.
    result = bundle / "result.json"
    result.write_text(result.read_text() + "\n# tampered\n", encoding="utf-8")
    assert PROMOTE.verify(evidence) != 0, "verify must flag a tampered promoted file"


# ---- hardened symlink handling (R2) ------------------------------------------


def test_a_broken_r2_symlink_is_recorded_not_dropped(tmp_path: Path) -> None:
    """The real OpenCode auth links are broken (target deleted); they must be
    enumerated and recorded, never silently skipped and never dereferenced."""
    job = make_job(tmp_path, broken_auth=True)
    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    link = next(
        e
        for e in manifest["files"]
        if e.get("entry_type") == "symlink"
    )
    assert link["link_target"] == "/run/secrets/evallab_zai_opencode_auth.json"
    assert link["action"] == "omitted" and link["rule"] == "R2"
    assert link["source_sha256"] == PROMOTE.sha256_bytes(
        link["link_target"].encode("utf-8")
    )
    assert not any(p.is_symlink() for p in bundle.rglob("*"))


def test_a_live_r2_symlink_target_is_never_read(tmp_path: Path) -> None:
    """A live credential link must be recorded by its link-target string; its
    target bytes must never be read, digested or written."""
    job = make_job(tmp_path)  # live auth.json -> host-secret.json
    bundle = tmp_path / "evidence" / job.name
    manifest = PROMOTE.promote(job, bundle)

    link = next(e for e in manifest["files"] if e.get("entry_type") == "symlink")
    secret = tmp_path / "host-secret.json"
    # Not the target content digest.
    assert link["source_sha256"] != PROMOTE.sha256_bytes(secret.read_bytes())
    assert link["source_sha256"] == PROMOTE.sha256_bytes(
        link["link_target"].encode("utf-8")
    )
    # And the target content never reached any promoted file.
    body = b"".join(b for _, b in promoted_bytes(bundle))
    assert AUTH_SECRET.encode() not in body


def test_a_non_r2_symlink_is_refused_and_never_disclosed(tmp_path: Path) -> None:
    """Any symlink outside an explicit omission rule must fail closed: promotion
    refuses to copy or dereference it, and its target content stays unread."""
    job = make_job(tmp_path, extra_symlink=tmp_path / "job" / "extra" / "link")
    bundle = tmp_path / "evidence" / job.name
    with pytest.raises(SystemExit):
        PROMOTE.promote(job, bundle)
    # Refused means the target was never read or copied: its content must not
    # appear anywhere, and no symlink may have been promoted.
    if bundle.exists():
        for name, body in promoted_bytes(bundle):
            assert b"opencode-outside-target-never-commit" not in body, name
        assert not any(p.is_symlink() for p in bundle.rglob("*"))


def test_a_tampered_symlink_omission_record_is_refused(tmp_path: Path) -> None:
    """verify() re-checks the symlink omission record source-free: a forged
    link_target must break the recorded digest."""
    job = make_job(tmp_path)
    evidence = tmp_path / "evidence"
    bundle = evidence / job.name
    PROMOTE.promote(job, bundle)

    manifest_path = bundle / "PROMOTION.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry.get("entry_type") == "symlink":
            entry["link_target"] = "/run/secrets/forged_target.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert PROMOTE.verify(evidence) != 0, "verify must refuse a forged symlink record"


# ---- v2 omission-schema tamper (delete / downgrade) ---------------------------


def _promote_bundle(tmp_path: Path) -> tuple[Path, Path]:
    job = make_job(tmp_path)
    evidence = tmp_path / "evidence"
    bundle = evidence / job.name
    PROMOTE.promote(job, bundle)
    return evidence, bundle


def test_v2_manifest_deleting_entry_type_is_refused(tmp_path: Path) -> None:
    """Source-free verify must not fall back to the legacy path when a v2
    manifest's omission record loses its entry_type: v2 requires it."""
    evidence, bundle = _promote_bundle(tmp_path)
    manifest_path = bundle / "PROMOTION.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    symlink = next(e for e in manifest["files"] if e.get("entry_type") == "symlink")
    del symlink["entry_type"]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert PROMOTE.verify(evidence) != 0, "verify must refuse a deleted entry_type"


def test_v2_manifest_deleting_link_target_is_refused(tmp_path: Path) -> None:
    """Deleting link_target from a v2 symlink omission must be caught."""
    evidence, bundle = _promote_bundle(tmp_path)
    manifest_path = bundle / "PROMOTION.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    symlink = next(e for e in manifest["files"] if e.get("entry_type") == "symlink")
    del symlink["link_target"]
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert PROMOTE.verify(evidence) != 0, "verify must refuse a deleted link_target"


def test_v2_manifest_version_downgrade_is_refused(tmp_path: Path) -> None:
    """Rewriting a v2 manifest's schema_version to v1 while keeping its v2
    omission records is a downgrade attempt and must be rejected."""
    evidence, bundle = _promote_bundle(tmp_path)
    manifest_path = bundle / "PROMOTION.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    manifest["schema_version"] = 1
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert PROMOTE.verify(evidence) != 0, "verify must refuse a version downgrade"
