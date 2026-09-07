import tomllib

from evallab.loader_shims import resolve_facet_task_name, resolve_tw_memory_conflict


def test_resolve_tw_memory_conflict():
    sample_tw = """# TerminalWorld task.toml
version = "1.0"

[environment]
cpus = 1
memory = "2G"
storage = "10G"
memory_mb = 4096
storage_mb = 10240
"""
    cleaned, policy = resolve_tw_memory_conflict(sample_tw)
    assert policy == "policy:memory_mb_precedence_dropped_legacy_memory"
    data = tomllib.loads(cleaned)
    assert "memory" not in data["environment"]
    assert data["environment"]["memory_mb"] == 4096
    assert data["environment"]["storage_mb"] == 10240


def test_resolve_tw_memory_conflict_noop_when_not_conflicting():
    sample = """[environment]
memory_mb = 2048
"""
    cleaned, policy = resolve_tw_memory_conflict(sample)
    assert policy == "unchanged"
    assert cleaned == sample


def test_resolve_facet_task_name():
    sample_facet = """[task]
name = "FACET-Terminal"
description = "Synthetic task"
version = "1.0"
"""
    cleaned, policy = resolve_facet_task_name(sample_facet, "task_000042")
    assert policy == "policy:namespaced_as_facet/task_000042"
    data = tomllib.loads(cleaned)
    assert data["task"]["name"] == "facet/task_000042"


def test_resolve_facet_custom_org():
    sample_facet = """[task]
name = "FACET-Terminal"
"""
    cleaned, policy = resolve_facet_task_name(sample_facet, "task_1", org="custom_org")
    assert policy == "policy:namespaced_as_custom_org/task_1"
    data = tomllib.loads(cleaned)
    assert data["task"]["name"] == "custom_org/task_1"
