"""Host task staging refusals: compose mount validation and staging-edge coverage.

HAR-363: the short compose mount syntax ``/etc:/h`` was already refused by
``_validate_compose_shape``, but the same bind written in long syntax
(``{type: bind, source: /etc, target: /h}``) passed validation because only
``str`` mount entries were inspected. A task package could therefore mount
host paths past the staging check. These tests pin refusal of host bind
mounts in both syntaxes, acceptance of named volumes in both syntaxes, and
the surrounding validator/staging refusals so the module stays guarded.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_task(root: Path, name: str = "shape-task") -> Path:
    """Minimal stageable task package; compose is appended by callers."""
    task = root / name
    _write(task / "task.toml", 'schema_version = "1.0"\n[task]\nname = "test/staging"\n')
    _write(task / "instruction.md", "# Task\nDo the thing.\n")
    _write(task / "environment" / "Dockerfile", "FROM alpine:3.20\nWORKDIR /app\n")
    (task / "tests").mkdir(parents=True, exist_ok=True)
    return task


def _make_compose_task(
    root: Path,
    services: dict,
    *,
    compose_text: str | None = None,
    name: str = "compose-task",
) -> Path:
    task = _make_task(root, name=name)
    text = compose_text if compose_text is not None else yaml.safe_dump(
        {"services": services}, sort_keys=False
    )
    _write(task / "environment" / "docker-compose.yaml", text)
    return task


def _stage(*args, **kwargs):  # type: ignore[no-untyped-def]
    from evallab.host_task_staging import stage_task_for_host

    return stage_task_for_host(*args, **kwargs)


# --------------------------------------------------------------------------
# HAR-363: long-syntax bind mounts refuse like short-syntax ones
# --------------------------------------------------------------------------


def test_stage_refuses_long_syntax_bind_mount_with_host_source(tmp_path: Path) -> None:
    """``{type: bind, source: /etc, target: /h}`` refuses like ``/etc:/h``.

    On main before the fix this staging call returned a manifest instead of
    raising, so a task package could mount host paths past the check.
    """
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "volumes": [{"type": "bind", "source": "/etc", "target": "/h"}],
            }
        },
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="forbidden host bind mount"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_long_syntax_bind_without_host_source(tmp_path: Path) -> None:
    """Long-syntax ``type: bind`` refuses even with a named-looking source."""
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "volumes": [{"type": "bind", "source": "data-volume", "target": "/data"}],
            }
        },
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="forbidden host bind mount"):
        _stage(task, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "source",
    ["/etc", "./host-dir", "../escape", "~/home-dir", "C:/host", "c:\\host"],
    ids=["absolute", "relative-dot", "parent-dot", "tilde", "drive-slash", "drive-backslash"],
)
def test_stage_refuses_long_syntax_mount_with_host_path_source(
    tmp_path: Path, source: str
) -> None:
    """A non-bind mapping whose source looks like a host path also refuses."""
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "volumes": [{"type": "volume", "source": source, "target": "/data"}],
            }
        },
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="forbidden host bind mount"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_long_syntax_docker_socket_mount(tmp_path: Path) -> None:
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    }
                ],
            }
        },
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="docker socket"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_accepts_long_syntax_named_volume(tmp_path: Path) -> None:
    """A named volume in long syntax (``type: volume``) still stages."""
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "volumes": [{"type": "volume", "source": "data-volume", "target": "/data"}],
            }
        },
        name="named-volume-task",
    )
    compose = yaml.safe_load(
        (task / "environment" / "docker-compose.yaml").read_text()
    )
    compose["volumes"] = {"data-volume": None}
    (task / "environment" / "docker-compose.yaml").write_text(
        yaml.safe_dump(compose, sort_keys=False), encoding="utf-8"
    )
    manifest = _stage(task, tmp_path / "staged")
    assert manifest.compose_present is True


def test_stage_accepts_mapping_mount_without_type_or_source(tmp_path: Path) -> None:
    """A sparse mapping mount entry is not confused for a host bind."""
    task = _make_compose_task(
        tmp_path,
        {"main": {"build": ".", "volumes": [{"target": "/data"}]}},
        name="sparse-mount-task",
    )
    manifest = _stage(task, tmp_path / "staged")
    assert manifest.compose_present is True


# --------------------------------------------------------------------------
# Compose shape refusals
# --------------------------------------------------------------------------


def test_stage_refuses_non_mapping_compose_document(tmp_path: Path) -> None:
    task = _make_compose_task(tmp_path, {}, compose_text="- not\n- a\n- mapping\n")
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="not a mapping"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_empty_services(tmp_path: Path) -> None:
    task = _make_compose_task(tmp_path, {}, compose_text="services: {}\n")
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="non-empty mapping"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_non_mapping_services(tmp_path: Path) -> None:
    task = _make_compose_task(tmp_path, {}, compose_text="services: []\n")
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="non-empty mapping"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_missing_main_service(tmp_path: Path) -> None:
    task = _make_compose_task(tmp_path, {"worker": {"image": "alpine:3.20"}})
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="'main' service"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_invalid_service_name(tmp_path: Path) -> None:
    task = _make_compose_task(
        tmp_path, {"main": {"image": "alpine:3.20"}, "_bad name": {"image": "x"}}
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="invalid compose service name"):
        _stage(task, destination)
    assert not destination.exists()


def test_stage_refuses_non_mapping_service_config(tmp_path: Path) -> None:
    task = _make_compose_task(tmp_path, {}, compose_text="services:\n  main: alpine\n")
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="must be a mapping"):
        _stage(task, destination)
    assert not destination.exists()


# --------------------------------------------------------------------------
# Networks and egress edge shapes
# --------------------------------------------------------------------------


def test_stage_records_dict_form_service_networks(tmp_path: Path) -> None:
    """Dict-form ``networks`` produces sorted manifest entries for main."""
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "networks": {"beta-net": None, "alpha-net": None},
            }
        },
        name="dict-networks-task",
    )
    manifest = _stage(task, tmp_path / "staged")
    assert manifest.main_networks == ("alpha-net", "beta-net")


def test_stage_records_main_only_compose_no_sidecar(tmp_path: Path) -> None:
    """A main-only compose has no sidecar networks recorded."""
    task = _make_compose_task(
        tmp_path, {"main": {"build": "."}}, name="main-only-task"
    )
    manifest = _stage(task, tmp_path / "staged")
    assert manifest.main_networks is None
    assert manifest.sidecar_networks is None


def test_stage_egress_refuses_scalar_main_networks(tmp_path: Path) -> None:
    """``attach_agent_egress`` refuses a non-list/dict networks declaration."""
    task = _make_compose_task(
        tmp_path,
        {},
        compose_text="services:\n  main:\n    build: .\n    networks: host\n",
        name="scalar-networks-task",
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="networks must be a list or mapping"):
        _stage(task, destination, attach_agent_egress=True)
    assert not destination.exists()


def test_stage_egress_appends_to_dict_main_networks(tmp_path: Path) -> None:
    """Dict-form networks gain the public egress attachment in place."""
    task = _make_compose_task(
        tmp_path,
        {
            "main": {
                "build": ".",
                "networks": {"internal-net": None},
            }
        },
        name="dict-egress-task",
    )
    manifest = _stage(task, tmp_path / "staged", attach_agent_egress=True)
    assert manifest.agent_public_egress is True
    staged = yaml.safe_load(
        (tmp_path / "staged" / "environment" / "docker-compose.yaml").read_text()
    )
    assert set(staged["services"]["main"]["networks"]) == {"internal-net", "default"}


def test_stage_egress_refuses_default_only_dict_networks(tmp_path: Path) -> None:
    """Proof of minimality catches a canonical document already on the default
    network: stripping the staging attachment cannot restore it, so staging
    refuses rather than silently accepting an egress-declared package."""
    task = _make_compose_task(
        tmp_path,
        {"main": {"build": ".", "networks": {"default": None}}},
        name="default-network-task",
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="beyond the requested"):
        _stage(task, destination, attach_agent_egress=True)
    assert not destination.exists()


def test_stage_egress_refuses_scalar_networks_document(tmp_path: Path) -> None:
    """List-invariant proof path: staging writes no compose change when the
    canonical document is already correct except for refusing scalar nets."""
    task = _make_compose_task(
        tmp_path,
        {},
        compose_text="services:\n  main:\n    build: .\n    networks: 42\n",
        name="int-networks-task",
    )
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="networks must be a list or mapping"):
        _stage(task, destination, attach_agent_egress=True)
    assert not destination.exists()


# --------------------------------------------------------------------------
# Source/destination guards and digest edges
# --------------------------------------------------------------------------


def test_stage_refuses_symlinked_destination(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    real = tmp_path / "real-dest"
    real.mkdir()
    link = tmp_path / "staged"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        _stage(task, link)


def test_stage_refuses_missing_source_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        _stage(tmp_path / "no-such-task", tmp_path / "staged")


def test_stage_refuses_source_without_task_toml(tmp_path: Path) -> None:
    task = tmp_path / "bare"
    _write(task / "instruction.md", "# Task\n")
    with pytest.raises(ValueError, match="task.toml missing"):
        _stage(task, tmp_path / "staged")


def test_task_payload_digest_refuses_missing_and_empty_dirs(tmp_path: Path) -> None:
    from evallab.host_task_staging import task_payload_digest

    with pytest.raises(ValueError, match="not found"):
        task_payload_digest(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="empty"):
        task_payload_digest(empty)


def test_stage_refuses_when_source_mutates_during_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source touched mid-stage fails the post-copy digest comparison."""
    import evallab.host_task_staging as staging

    task = _make_task(tmp_path, name="mutating-task")
    real_adapt = staging.adapt_task_toml_for_host

    def mutating_adapt(text, **kwargs):  # type: ignore[no-untyped-def]
        _write(task / "sneaky.txt", "planted mid-stage\n")
        return real_adapt(text, **kwargs)

    monkeypatch.setattr(staging, "adapt_task_toml_for_host", mutating_adapt)
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="mutated during staging"):
        _stage(task, destination)
    assert not destination.exists()


# --------------------------------------------------------------------------
# Direct helper contracts (defensive branches unreachable through staging)
# --------------------------------------------------------------------------


def test_without_staging_changes_tolerates_non_mapping_shapes() -> None:
    from evallab.host_task_staging import _without_staging_changes

    data = {"services": "oops", "version": "3"}
    assert _without_staging_changes(data, strip_pins=True, strip_egress=True) is not data
    assert _without_staging_changes(data, strip_pins=True, strip_egress=True) == data
    odd = {"services": {"main": "oops"}}
    assert _without_staging_changes(odd, strip_pins=True, strip_egress=True) == odd


def test_service_networks_helper_edge_shapes() -> None:
    from evallab.host_task_staging import _service_networks

    assert _service_networks({"services": {"main": "oops"}}, "main") is None
    assert _service_networks({"services": {}}, "main") is None


def test_verify_dockerfile_change_refuses_line_count_and_content_drift() -> None:
    from evallab.host_task_staging import _verify_dockerfile_change

    with pytest.raises(ValueError, match="line count"):
        _verify_dockerfile_change("FROM a\nRUN x\n", "FROM --platform=linux/amd64 a\n", "Dockerfile")
    with pytest.raises(ValueError, match="more than the FROM platform flag"):
        _verify_dockerfile_change(
            "FROM a\nRUN x\n",
            "FROM --platform=linux/amd64 b\nRUN x\n",
            "Dockerfile",
        )


def test_validate_compose_shape_refuses_long_bind_directly() -> None:
    """The validator itself refuses the long-syntax bind the report cites."""
    from evallab.host_task_staging import _validate_compose_shape

    with pytest.raises(ValueError, match="forbidden host bind mount"):
        _validate_compose_shape(
            {
                "services": {
                    "main": {
                        "image": "x",
                        "volumes": [{"type": "bind", "source": "/etc", "target": "/h"}],
                    }
                }
            },
            Path("c.yaml"),
        )
