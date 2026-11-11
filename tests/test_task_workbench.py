"""Native package security regressions retained after Workbench retirement."""

from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

from evallab.task_lint import CandidateSource, check_package

FIXTURES = Path(__file__).parent / "fixtures/task_workbench"
SOURCE = CandidateSource("local/security-fixture", "local/security-fixture@1.0.0", "MIT")


@pytest.fixture
def task(tmp_path: Path) -> Path:
    destination = tmp_path / "task"
    shutil.copytree(FIXTURES / "valid", destination)
    return destination


def _codes(task: Path) -> set[str]:
    return {item.code for item in check_package(task, source=SOURCE)}


def test_native_checks_are_read_only_and_do_not_issue_certificates(task: Path) -> None:
    before = {path: path.read_bytes() for path in task.rglob("*") if path.is_file()}
    assert check_package(task, source=SOURCE) == ()
    assert {path: path.read_bytes() for path in task.rglob("*") if path.is_file()} == before
    assert sorted(path.name for path in task.parent.iterdir()) == ["task"]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing-files", "required_file_missing"),
        ("golden-symlink", "symlink_unsupported"),
        ("json-copy-leak", "agent_image_hidden_leak"),
        ("hidden-leak", "golden_data_leak"),
        ("network-use", "runtime_network_use"),
        ("unpinned-dependency", "base_image_unpinned"),
        ("networked-verifier", "verifier_network_not_isolated"),
        ("step-verifier-network", "unsupported_task_configuration"),
        ("build-context-script", "build_network_use"),
        ("verifier-build-script", "build_network_use"),
        ("verifier-compose-escape", "custom_compose_unsupported"),
        ("verifier-uv-sync", "build_network_use"),
    ],
)
def test_retained_static_security_cases(task: Path, case: str, code: str) -> None:
    case_root = FIXTURES / "cases" / case
    manifest_path = case_root / "case.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        for relative in manifest.get("remove", []):
            (task / relative).unlink()
        if "symlink" in manifest:
            (task / manifest["symlink"]).symlink_to(manifest["target"])
    for path in case_root.rglob("*"):
        if path.is_file() and path.name not in {"case.json", "controls.json"}:
            destination = task / path.relative_to(case_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    diagnostics = check_package(task, source=SOURCE)
    assert code in {item.code for item in diagnostics}
    assert all("ALPHA-BETA-GAMMA" not in item.message for item in diagnostics)


@pytest.mark.parametrize(
    "relative",
    [
        "environment/compose.yaml",
        "environment/docker-compose.override.yaml",
        "tests/docker-compose.yaml",
        "tests/docker-compose.yml",
        "tests/compose.yml",
        "tests/docker-compose.override.yml",
        "tests/fixtures/docker-compose.yaml",
    ],
)
def test_unmodelled_compose_files_are_refused(task: Path, relative: str) -> None:
    destination = task / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("services:\n  main:\n    network_mode: bridge\n")
    assert "custom_compose_unsupported" in _codes(task)


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("environment/.env", "compose_env_file_unsupported"),
        ("tests/.env", "compose_env_file_unsupported"),
        ("tests/.env.local", "compose_env_file_unsupported"),
        ("environment/.dockerignore", "build_context_ignore_unsupported"),
        ("tests/.dockerignore", "build_context_ignore_unsupported"),
        ("environment/binary.bin", "build_context_unreadable"),
        ("tests/binary.bin", "build_context_unreadable"),
    ],
)
def test_interpreted_or_unreadable_context_files_fail_closed(
    task: Path, relative: str, code: str
) -> None:
    (task / relative).write_bytes(b"\xff" if relative.endswith(".bin") else b"KEY=value\n")
    assert code in _codes(task)


@pytest.mark.parametrize(
    "relative", ["tests/composer.json", "environment/.envrc", "tests/service-compose.yaml"]
)
def test_uninterpreted_lookalike_names_remain_allowed(task: Path, relative: str) -> None:
    (task / relative).write_text("{}\n")
    assert _codes(task) == set()


@pytest.mark.parametrize(
    "command",
    [
        "uv sync --frozen",
        "uv add ruff",
        "uv lock",
        "npm i left-pad",
        "pnpm i",
        "npx cowsay hello",
        "poetry install --no-root",
        "pipenv install",
        "bundle install",
        "composer install",
        "conda install -y numpy",
        "micromamba install -y numpy",
        "brew install jq",
        "pacman -Syu --noconfirm",
        "dotnet restore",
        "dotnet add package Newtonsoft.Json",
        "mvn install -DskipTests",
        "./gradlew build",
        "gradle build",
        "pip download requests",
        "cargo fetch",
        "go mod download",
    ],
)
def test_online_build_install_commands_are_rejected(task: Path, command: str) -> None:
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + f"RUN {command}\n")
    assert any(
        item.code == "build_network_use" and item.path == "tests/Dockerfile"
        for item in check_package(task, source=SOURCE)
    )


@pytest.mark.parametrize(
    "command",
    [
        "npm init -y",
        "cargo build --offline",
        "python -m compileall /tests",
        "chmod +x /tests/test.sh",
        "mkdir -p /app/output",
    ],
)
def test_offline_build_commands_remain_allowed(task: Path, command: str) -> None:
    dockerfile = task / "tests/Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + f"RUN {command}\n")
    assert _codes(task) == set()


@pytest.mark.parametrize(
    ("addition", "location"),
    [
        (
            '[environment.mcp_servers.docs]\nurl = "https://example.invalid"\n',
            "environment.mcp_servers",
        ),
        ('[verifier.collect]\ncommand = "true"\n', "verifier.collect"),
        (
            '[[task.authors]]\nname = "Other"\naffiliation = "Somewhere"\n',
            "task.authors[1].affiliation",
        ),
        ('multi_step_reward_strategy = "final"\n', "multi_step_reward_strategy"),
        ('[environment.healthcheck]\ntest = "true"\n', "environment.healthcheck"),
        ("[solution]\nseed = 1\n", "solution.seed"),
    ],
)
def test_unmodelled_configuration_is_not_silently_accepted(
    task: Path, addition: str, location: str
) -> None:
    manifest = task / "task.toml"
    original = manifest.read_text()
    manifest.write_text(
        original + "\n" + addition if addition.startswith("[") else addition + original
    )
    diagnostics = check_package(task, source=SOURCE)
    assert any(
        item.code == "unsupported_task_configuration"
        and item.message.startswith(location + " is outside")
        for item in diagnostics
    )


@pytest.mark.parametrize(
    "key,value",
    [("os", '"windows"'), ("gpus", "1"), ("gpus", "true"), ("env", '{ TOKEN = "${TOKEN}" }')],
)
def test_inert_environment_keys_refuse_active_values(task: Path, key: str, value: str) -> None:
    manifest = task / "task.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'network_mode = "no-network"', f'network_mode = "no-network"\n{key} = {value}', 1
        )
    )
    assert "unsupported_task_configuration" in _codes(task)


@pytest.mark.parametrize(
    ("addition", "code"),
    [
        ("\n[verifier.environment]\ncpus = 1\n", "verifier_network_not_isolated"),
        ('\n[verifier.environment]\nnetwork_mode = "public"\n', "verifier_network_not_isolated"),
        (
            '\n[verifier.environment]\nnetwork_mode = "no-network"\ndocker_image = "x"\n',
            "unsupported_task_configuration",
        ),
    ],
)
def test_verifier_environment_does_not_inherit_isolation(
    task: Path, addition: str, code: str
) -> None:
    manifest = task / "task.toml"
    manifest.write_text(manifest.read_text() + addition)
    assert code in _codes(task)


def test_verifier_phase_cannot_reopen_network(task: Path) -> None:
    manifest = task / "task.toml"
    manifest.write_text(
        manifest.read_text().replace("[verifier]\n", '[verifier]\nnetwork_mode = "public"\n')
    )
    assert "verifier_phase_network_not_isolated" in _codes(task)


@pytest.mark.parametrize(
    "field,value",
    [
        ("external", True),
        ("driver", "overlay"),
        ("driver_opts", {"x": "y"}),
        ("name", "global"),
        ("ipam", {}),
        ("attachable", True),
        ("labels", ["x=y"]),
        ("enable_ipv6", True),
        ("internal", False),
    ],
)
def test_compose_network_properties_fail_closed(task: Path, field: str, value: object) -> None:
    doc = {
        "services": {
            "main": {"build": ".", "networks": ["local"]},
            "sidecar": {"build": ".", "networks": ["local"]},
        },
        "networks": {"local": {"internal": True, field: value}},
    }
    (task / "environment/docker-compose.yaml").write_text(yaml.safe_dump(doc))
    assert "compose_networks_unsupported" in _codes(task)


@pytest.mark.parametrize(
    "attachment",
    [
        None,
        ["wrong"],
        ["local", "other"],
        {"local": None},
        {"local": {}},
        {"local": {"aliases": ["alias"]}},
    ],
)
def test_compose_network_attachment_must_be_exact(task: Path, attachment: object) -> None:
    main = {"build": "."}
    if attachment is not None:
        main["networks"] = attachment
    doc = {
        "services": {"main": main, "sidecar": {"build": ".", "networks": ["local"]}},
        "networks": {"local": {"internal": True}},
    }
    (task / "environment/docker-compose.yaml").write_text(yaml.safe_dump(doc))
    assert "compose_networks_unsupported" in _codes(task)


@pytest.mark.parametrize(
    "fragment,code",
    [
        ({"volumes": ["/:/host"]}, "compose_volume_escape"),
        ({"command": "curl https://example.invalid"}, "compose_service_key_unsupported"),
        ({"entrypoint": "/bin/sh"}, "compose_service_key_unsupported"),
        ({"cap_add": ["SYS_ADMIN"]}, "compose_service_key_unsupported"),
        ({"devices": ["/dev/null:/dev/escape"]}, "compose_service_key_unsupported"),
        ({"env_file": ".env"}, "compose_service_key_unsupported"),
        ({"environment": {"SECRET": "value"}}, "compose_sidecar_env_invalid"),
    ],
)
def test_compose_execution_surfaces_remain_confined(task: Path, fragment: dict, code: str) -> None:
    doc = {
        "services": {"main": {"build": "."}, "sidecar": {"build": ".", **fragment}},
        "volumes": {"local": None},
    }
    (task / "environment/docker-compose.yaml").write_text(yaml.safe_dump(doc))
    assert code in _codes(task)


@pytest.mark.parametrize(
    "name",
    [
        "no-org",
        "../evil",
        "org/..name",
        "org/name..",
        ".org/name",
        "org/.name",
        "/name",
        "org/",
        "org/name/extra",
        "org name/task",
    ],
)
def test_invalid_package_identity_is_refused(task: Path, name: str) -> None:
    manifest = task / "task.toml"
    config = tomllib.loads(manifest.read_text())
    manifest.write_text(manifest.read_text().replace(config["task"]["name"], name))
    assert "task_name_invalid" in _codes(task)


@pytest.mark.parametrize(
    "name", ["evallab/name", "local-lab/v2-agentic-fixture", "org.sub_name-1/task.name-2"]
)
def test_valid_package_identity_is_accepted(task: Path, name: str) -> None:
    manifest = task / "task.toml"
    config = tomllib.loads(manifest.read_text())
    manifest.write_text(manifest.read_text().replace(config["task"]["name"], name))
    assert _codes(task) == set()


@pytest.mark.parametrize(
    ("service", "command", "code"),
    [
        ("mcp-server", "cp /logs/out.txt /app/output/result.txt", "collect_service_invalid"),
        ("main", "pg_dump > /tmp/state.sql", "verifier_collect_unsupported"),
        ("main", "cat /logs/a | tee /app/output/result.txt", "verifier_collect_unsupported"),
        ("main", "cp /logs/../etc/passwd /app/output/result.txt", "verifier_collect_unsupported"),
        ("main", "cp /logs/*.txt /app/output/result.txt", "verifier_collect_unsupported"),
        ("main", "cp /logs/$(id).txt /app/output/result.txt", "verifier_collect_unsupported"),
        ("main", "cp /logs/out.txt /app/output/`id`.txt", "verifier_collect_unsupported"),
        ("main", "cp /logs/out.txt /app/undeclared.txt", "verifier_collect_unsupported"),
        ("main", "cp /solution/solve.sh /app/output/result.txt", "verifier_collect_unsupported"),
    ],
)
def test_collect_refuses_unsafe_hooks(task: Path, service: str, command: str, code: str) -> None:
    manifest = task / "task.toml"
    with manifest.open("a") as handle:
        handle.write(
            f"\n[[verifier.collect]]\nservice = {json.dumps(service)}\ncommand = {json.dumps(command)}\n"
        )
    assert code in _codes(task)


def test_collect_accepts_declared_guarded_copy(task: Path) -> None:
    with (task / "task.toml").open("a") as handle:
        handle.write(
            '\n[[verifier.collect]]\nservice = "main"\n'
            'command = "if [ ! -f /app/output/result.txt ] && [ -f /logs/agent/result.txt ]; '
            'then cp /logs/agent/result.txt /app/output/result.txt; fi"\n'
        )
    assert check_package(task, source=SOURCE) == ()


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("OPENAI_API_KEY", "sk-1234567890abcdef", "verifier_env_literal_secret"),
        ("OPENAI_API_KEY", "${OPENAI_API_KEY:-fallback_secret}", "verifier_env_literal_secret"),
        ("UNAUTHORIZED", "${UNAUTHORIZED}", "verifier_credential_unauthorized"),
        ("OPENAI_API_KEY", "${UNAUTHORIZED}", "verifier_credential_unauthorized"),
        ("OPENAI_API_KEY", "${SIMULATED_USER}", "verifier_credential_alias_unsupported"),
        ("OPENAI_API_KEY", "${OPENAI_API_KEY}", None),
    ],
)
def test_verifier_credentials_require_exact_authorized_placeholders(
    task: Path, name: str, value: str, code: str | None
) -> None:
    with (task / "task.toml").open("a") as handle:
        handle.write(f"\n[verifier.env]\n{name} = {json.dumps(value)}\n")
    source = CandidateSource(
        "local/fixture", "v1", "MIT", credentials=("SIMULATED_USER", "OPENAI_API_KEY")
    )
    findings = check_package(task, source=source)
    if code is None:
        assert findings == ()
    else:
        assert code in {item.code for item in findings}


def test_offline_build_proof_binds_lock_and_cannot_authorize_network(task: Path) -> None:
    dockerfile = task / "tests/Dockerfile"
    base = dockerfile.read_text()
    dockerfile.write_text(base + "\nRUN uv sync --frozen --offline\n")
    assert "build_network_use" in _codes(task)
    proof_path = task / "tests/build-proof.json"
    proof_path.write_text("{malformed-json")
    assert "build_proof_invalid" in _codes(task)
    proof = {
        "schema_version": "1.0",
        "kind": "offline_build_proof",
        "ecosystem": "python",
        "lockfile": "uv.lock",
        "lockfile_digest": "sha256:" + "f" * 64,
        "pinned_dependencies": {"pkg": "1.0.0@sha256:" + "a" * 64},
        "reviewed_by": "eval-lab-reviewer",
    }
    proof_path.write_text(json.dumps(proof))
    assert "build_proof_lockfile_missing" in _codes(task)
    (task / "tests/uv.lock").write_text("lock-data-v1\n")
    assert "build_proof_lockfile_mismatch" in _codes(task)
    proof["lockfile_digest"] = "sha256:" + hashlib.sha256(b"lock-data-v1\n").hexdigest()
    proof_path.write_text(json.dumps(proof))
    assert check_package(task, source=SOURCE) == ()
    for version in ("latest", "*", ">=1.0.0", "^2.0", "~=1.4"):
        proof["pinned_dependencies"] = {"pkg": version}
        proof_path.write_text(json.dumps(proof))
        assert "build_proof_unpinned_dependency" in _codes(task)
    proof["pinned_dependencies"] = {"pkg": "1.0.0@sha256:" + "a" * 64}
    proof_path.write_text(json.dumps(proof))
    for command in (
        "curl https://example.invalid/payload",
        "pip install --no-index --require-hashes -r https://example.invalid/requirements.txt",
    ):
        dockerfile.write_text(base + f"\nRUN {command}\n")
        assert "build_network_use" in _codes(task)


@pytest.mark.parametrize(
    ("environment", "verifier", "expected"),
    [
        ('network_mode = "no-network"', "", ("no-network", "no-network")),
        (
            'network_mode = "no-network"',
            "\n[verifier.environment]\ncpus = 1\n",
            ("public", "public"),
        ),
        (
            'network_mode = "public"',
            '\n[verifier.environment]\nnetwork_mode = "no-network"\n',
            ("no-network", "no-network"),
        ),
        ('network_mode = "no-network"', '\nnetwork_mode = "public"', ("no-network", "public")),
    ],
)
def test_verifier_network_resolution_matches_native_harbor(
    task: Path, environment: str, verifier: str, expected: tuple[str, str]
) -> None:
    from evallab.task_lint import _effective_verifier_network

    document = (task / "task.toml").read_text().replace('network_mode = "no-network"', environment)
    if verifier.startswith("\n["):
        document += verifier
    else:
        document = document.replace("[verifier]", "[verifier]" + verifier)
    config = tomllib.loads(document)
    assert _effective_verifier_network(config)[:2] == expected
    native = pytest.importorskip("harbor.models.task.config")
    from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
    from harbor.trial.network_policy import resolve_verifier_phase_policy

    task_config = native.TaskConfig.model_validate(config)
    resolved = resolve_effective_verifier_env_config(task_config, None)
    baseline = resolved.resolve_baseline()
    phase = resolve_verifier_phase_policy(task_config, None, baseline=baseline)
    assert (baseline.network_mode.value, phase.network_mode.value) == expected


def test_internal_mcp_bridge_is_accepted_without_certification(task: Path) -> None:
    with (task / "task.toml").open("a") as handle:
        handle.write(
            '\n[[environment.mcp_servers]]\nname = "local-tools"\n'
            'transport = "streamable-http"\nurl = "http://mcp-server:8000/mcp"\n'
        )
    (task / "environment/docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    networks: [mcp-net]\n"
        "  mcp-server:\n    build: .\n    networks: [mcp-net]\n"
        "networks:\n  mcp-net:\n    internal: true\n"
    )
    assert check_package(task, source=SOURCE) == ()
    assert not (task / "certification.json").exists()


def test_package_symlinks_are_refused_before_reading_external_bytes(
    task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = task.parent / "external.toml"
    external.write_text("private bytes must not enter package checks")
    manifest = task / "task.toml"
    manifest.unlink()
    manifest.symlink_to(external)
    read_text = Path.read_text

    def confined_read(path: Path, *args, **kwargs):
        assert path.resolve() != external
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", confined_read)
    assert {"symlink_unsupported", "path_escape"} <= _codes(task)


def _packet_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _packet_digest(value: dict) -> str:
    return "sha256:" + hashlib.sha256(_packet_bytes(value)).hexdigest()


@pytest.fixture
def retained_packet(task: Path):
    """Historical registration binding fixture, never a Gold certificate."""
    from evallab.registry import compute_task_digests

    repo = task.parent
    root = repo / "research/registration/candidates/historical"
    root.mkdir(parents=True)
    binding = {
        "task_id": "uppercase-fixture",
        "task_version": "1.0.0",
        "task_path": "task",
        "package_digest": compute_task_digests(task).package,
    }
    candidate = {
        **{key: binding[key] for key in ("task_id", "task_version", "task_path")},
        "candidate_id": "candidate-" + "0" * 24,
        "workbench_version": "m049-v1",
        "digests": {"package": "sha256:" + "a" * 64, "registry_package": binding["package_digest"]},
    }
    candidate["candidate_record_digest"] = _packet_digest(candidate)
    (root / "candidate.json").write_bytes(_packet_bytes(candidate))
    (root / "retained.json").write_bytes(b"historical test evidence\n")
    artifact = {
        "path": (root / "retained.json").relative_to(repo).as_posix(),
        "digest": "sha256:" + hashlib.sha256(b"historical test evidence\n").hexdigest(),
    }
    plan = [
        *({"control_id": f"oracle-{i}", "kind": "oracle"} for i in range(3)),
        *({"control_id": f"nop-{i}", "kind": "nop"} for i in range(2)),
        *({"control_id": f"invalid-{i}", "kind": "adversarial"} for i in range(3)),
        {"control_id": "fair-alternative", "kind": "fair_alternative"},
        {"control_id": "please-hack", "kind": "please_hack"},
    ]
    observations = [
        {
            "control_id": entry["control_id"],
            "status": "completed",
            "exception_type": None,
            "reward": 1.0 if entry["kind"] in {"oracle", "fair_alternative"} else 0.0,
            "verifier_output_digest": "sha256:" + "c" * 64,
            "evidence_digest": artifact["digest"],
        }
        for entry in plan
    ]
    body = {
        "kind": "task_workbench_certification",
        "certified": True,
        "status": "certified_for_review",
        "workbench_version": "m049-v1",
        "candidate_id": candidate["candidate_id"],
        "candidate_record_digest": candidate["candidate_record_digest"],
        "task_binding": {**binding, "candidate_package_digest": candidate["digests"]["package"]},
        "generator_identity": {
            "execution": "local",
            "model": None,
            "code_digest": "sha256:" + "a" * 64,
        },
        "validator_identity": {
            "execution": "local",
            "model": None,
            "code_digest": "sha256:" + "b" * 64,
        },
        "retained_evidence": [{**artifact, "control_id": "please-hack"}],
        "retained_replays": [
            {**artifact, "control_id": name} for name in ("fair-alternative", "please-hack")
        ],
        "control_plan": plan,
        "control_bundle": {"observations": observations},
        "control_summary": {
            "oracle_runs": 3,
            "nop_runs": 2,
            "invalid_probe_runs": 3,
            "fair_alternative_runs": 1,
            "please_hack_runs": 1,
            "result_digests": [_packet_digest(item) for item in observations],
        },
        "diagnostics": [],
        "check_vector": {
            **dict.fromkeys(
                (
                    "all_controls_completed",
                    "static",
                    "oracle_exact_1_x3",
                    "oracle_stable_output",
                    "nop_exact_0_x2",
                    "invalid_outputs_rejected",
                    "fair_alternative_exact_1",
                    "please_hack_executed",
                    "leakage_scan_clean",
                    "isolation",
                ),
                True,
            ),
            "hack_detected": False,
        },
        "axes": {
            name: {"status": "passed", "reason": "historical package check"}
            for name in (
                "task_correctness",
                "verifier_soundness",
                "verifier_completeness",
                "solvability",
            )
        }
        | {
            name: {"status": "not_assessed", "reason": "not a Gold certificate"}
            for name in ("difficulty_calibration", "realism_review")
        },
    }
    packet = root / "certification.json"
    _rewrite_packet(packet, body)
    return repo, packet, binding


def _rewrite_packet(packet: Path, body: dict) -> None:
    body.pop("certification_id", None)
    body["certification_id"] = "cert-" + hashlib.sha256(_packet_bytes(body)).hexdigest()[:24]
    packet.write_bytes(_packet_bytes(body))


def test_retained_packet_binding_survives_registry_reload_and_detects_byte_tamper(
    task: Path, retained_packet
) -> None:
    from evallab.registry import (
        TaskCertificationError,
        certification_envelope_from_packet,
        compute_task_digests,
        verify_certification_packet,
    )
    from evallab.schemas import TaskRegistryRecord

    repo, packet, binding = retained_packet
    envelope = certification_envelope_from_packet(repo, packet, **binding)
    record = TaskRegistryRecord(
        task_id="uppercase-fixture",
        task_family="uppercase-fixture",
        version="1.0.0",
        task_path="task",
        digests=compute_task_digests(task),
        source_uri="local/fixture",
        source_ref="v1",
        license="MIT",
        provenance_zone="03-synthetic",
        is_synthetic=True,
        certification=envelope,
        state="candidate",
        state_reason="historical_packet_bound_pending_admission",
        allowed_uses=["measurement"],
    )
    reloaded = TaskRegistryRecord.model_validate_json(record.model_dump_json())
    verify_certification_packet(repo, reloaded)
    assert reloaded.state == "candidate"
    packet.write_bytes(packet.read_bytes() + b" ")
    with pytest.raises(TaskCertificationError):
        verify_certification_packet(repo, reloaded)


@pytest.mark.parametrize("field", ["task_id", "task_version", "task_path", "package_digest"])
def test_retained_packet_cannot_be_replayed_for_a_different_identity(
    retained_packet, field: str
) -> None:
    from evallab.registry import TaskCertificationError, certification_envelope_from_packet

    repo, packet, binding = retained_packet
    binding[field] = "different-identity"
    with pytest.raises(TaskCertificationError):
        certification_envelope_from_packet(repo, packet, **binding)


@pytest.mark.parametrize(
    "mutation",
    ["circular", "missing-replay", "vector", "summary", "version", "leakage", "evidence"],
)
def test_retained_packet_rejects_tampered_or_contradictory_claims(
    retained_packet, mutation: str
) -> None:
    from evallab.registry import TaskCertificationError, certification_envelope_from_packet

    repo, packet, binding = retained_packet
    body = json.loads(packet.read_bytes())
    if mutation == "circular":
        body["generator_identity"] = body["validator_identity"]
    elif mutation == "missing-replay":
        body["retained_replays"] = [
            item for item in body["retained_replays"] if item["control_id"] != "please-hack"
        ]
    elif mutation == "vector":
        body["check_vector"]["oracle_exact_1_x3"] = False
    elif mutation == "summary":
        body["control_summary"]["oracle_runs"] = 99
    elif mutation == "version":
        body["workbench_version"] = "m999-v1"
    elif mutation == "leakage":
        body["diagnostics"].append(
            {
                "severity": "error",
                "code": "golden_data_leak",
                "classification": "task_defect",
                "path": "instruction.md",
                "message": "semantic tamper",
            }
        )
    else:
        (packet.parent / "retained.json").write_text("tampered")
    _rewrite_packet(packet, body)
    with pytest.raises(TaskCertificationError):
        certification_envelope_from_packet(repo, packet, **binding)
