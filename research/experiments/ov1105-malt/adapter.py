#!/usr/bin/env python3
"""Convert pinned original MALT sources to DetBench without model calls.

Usage: python adapter.py --source-manifest manifest.json --output /cache/malt
Requires label-map.json beside this file and pyarrow for Parquet inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORIES = {"metr-evals/malt-public": "malt-public", "metr-evals/malt-transcripts-public": "malt-transcripts-public"}
TRANSFORMS = {"irrelevant_detail", "language_mixing", "summarize", "vague_cot"}


class ConversionError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_helpers() -> Any:
    # The integrated file lives in research/experiments/ov1105-malt. Workers
    # also run from temporary output with the repository as their cwd.
    roots = [Path(__file__).resolve(), Path.cwd()]
    for root in roots:
        for parent in [root, *root.parents]:
            target = parent / "research/experiments/ov1104-detbench/detbench_lib.py"
            if target.is_file():
                spec = importlib.util.spec_from_file_location("malt_detbench_lib", target)
                if spec is None or spec.loader is None:
                    raise RuntimeError("Cannot load DetBench helpers")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("Run within eval-lab or integrate beside ov1104-detbench")


@dataclass(frozen=True)
class Source:
    repo: str
    revision: str
    relative: str
    path: Path
    digest: str
    expected_rows: int | None
    split: str

    @property
    def rank(self) -> int:
        return 0 if self.repo.endswith("/malt-public") else 1

    def receipt(self) -> dict[str, Any]:
        return {"repo_id": self.repo, "revision": self.revision, "relative_path": self.relative,
                "local_path": str(self.path), "sha256": self.digest, "split": self.split}


def manifest_files(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    files = [dict(item) for item in manifest.get("files", [])]
    datasets = manifest.get("datasets", [])
    if isinstance(datasets, Mapping):
        datasets = [dict(value, repo_id=key) for key, value in datasets.items()]
    for dataset in datasets:
        for item in dataset.get("files", []):
            files.append({"repo_id": dataset.get("repo_id", dataset.get("repo")),
                          "revision": dataset.get("revision"), **item})
    return files


def sources_from_manifest(manifest: Mapping[str, Any], base: Path) -> tuple[list[Source], list[dict[str, Any]]]:
    selected: list[Source] = []
    skipped = []
    seen = set()
    for item in manifest_files(manifest):
        repo = item.get("repo_id", item.get("repo"))
        relative = str(item.get("relative_path", item.get("path", "")))
        config = item.get("config", item.get("config_name", "default"))
        reason = None
        if repo not in REPOSITORIES:
            reason = "unsupported_repository"
        elif config != "default" or relative.split("/", 1)[0] in TRANSFORMS:
            reason = "transformed_configuration"
        elif not relative.startswith("data/"):
            reason = "not_card_selected_default_data"
        elif Path(relative).suffix not in {".parquet", ".jsonl", ".json"}:
            reason = "not_row_data"
        if reason:
            skipped.append({"repo_id": repo, "relative_path": relative, "reason": reason})
            continue
        path_value = item.get("local_path", item.get("cache_path"))
        if not path_value:
            raise ValueError(f"Missing local_path for {repo}/{relative}")
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        if path in seen:
            continue
        seen.add(path)
        revision = item.get("revision")
        digest = item.get("sha256")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError(f"Missing pinned revision for {path}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Missing SHA-256 for {path}")
        if not path.is_file():
            raise ValueError(f"Missing source file: {path}")
        size = item.get("bytes", item.get("size_bytes", item.get("size")))
        if size is not None and path.stat().st_size != int(size):
            raise ValueError(f"Source size mismatch: {path}")
        if sha256(path) != digest:
            raise ValueError(f"Source SHA-256 mismatch: {path}")
        split = item.get("split") or ("public" if repo.endswith("/malt-public") else "transcripts")
        expected_split = "public" if repo.endswith("/malt-public") else "transcripts"
        if split != expected_split:
            raise ValueError(f"Unexpected native split {split!r} for {repo}")
        expected_rows = item.get("row_count")
        selected.append(Source(repo, revision, relative, path, digest,
                               None if expected_rows is None else int(expected_rows), split))
    if not selected:
        raise ValueError("Manifest contains no original/default MALT row files")
    return sorted(selected, key=lambda value: (value.rank, value.repo, value.relative)), skipped


def rows(source: Source, *, metadata_only: bool = False) -> Iterator[dict[str, Any]]:
    if source.path.suffix == ".parquet":
        import pyarrow.parquet as pq
        parquet = pq.ParquetFile(source.path)
        if source.expected_rows is not None and parquet.metadata.num_rows != source.expected_rows:
            raise ValueError(f"Parquet row count mismatch: {source.path}")
        columns = None
        if metadata_only:
            names = parquet.schema_arrow.names
            columns = ["metadata"] if "metadata" in names else [key for key in
                       ("run_id", "task_id", "labels", "manually_reviewed", "run_source", "public", "model", "has_chain_of_thought") if key in names]
        for batch in parquet.iter_batches(batch_size=1 if not metadata_only else 512, columns=columns):
            yield from batch.to_pylist()
    elif source.path.suffix == ".jsonl":
        with source.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("Source row must be an object")
                    yield value
    else:
        payload = json.loads(source.path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            payload = payload.get("rows", payload.get("data"))
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ValueError("JSON source must contain an array of row objects")
        yield from payload


def metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    nested = row.get("metadata")
    return dict(nested) if isinstance(nested, Mapping) else {key: value for key, value in row.items() if key not in {"nodes", "samples"}}


def identity(meta: Mapping[str, Any]) -> tuple[str, str, str]:
    source, run_id, task = meta.get("run_source"), meta.get("run_id"), meta.get("task_id")
    if not isinstance(source, str) or not source or run_id is None or isinstance(run_id, bool) or not isinstance(task, str) or not task:
        raise ConversionError("missing_or_malformed_identity")
    if not isinstance(run_id, (str, int)) or (isinstance(run_id, str) and not run_id):
        raise ConversionError("missing_or_malformed_identity")
    return source, str(run_id), task


def load_label_map(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    for key in ("version", "positive_labels", "negative_labels", "excluded_labels", "policies", "provenance"):
        if key not in result:
            raise ValueError(f"label-map.json lacks {key}")
    for key in ("positive_labels", "negative_labels"):
        if not isinstance(result[key], list) or any(not isinstance(label, str) for label in result[key]):
            raise ValueError(f"{key} must be a list of strings")
    if set(result["positive_labels"]) & set(result["negative_labels"]):
        raise ValueError("Positive and negative label sets overlap")
    if not isinstance(result["excluded_labels"], (dict, list)) or not isinstance(result["policies"], dict) or not isinstance(result["provenance"], list):
        raise ValueError("Invalid label-map contract")
    return result


def classify(meta: Mapping[str, Any], label_map: Mapping[str, Any]) -> dict[str, Any]:
    raw = meta.get("labels")
    result: dict[str, Any] = {"label": None, "reason": None, "manual_review_required": False,
                              "uncertainty": [], "raw_labels": raw, "manually_reviewed": meta.get("manually_reviewed")}
    policies = label_map["policies"]
    natural_sources = policies.get("natural_run_sources", ["unprompted"])
    if not isinstance(natural_sources, list) or any(not isinstance(value, str) for value in natural_sources):
        raise ValueError("natural_run_sources must be a list of strings")
    if meta.get("run_source") not in natural_sources:
        result["reason"] = "non_natural_or_unknown_source"
        if meta.get("run_source") != "prompted":
            result["manual_review_required"] = True
            result["uncertainty"].append("unknown_or_missing_source_group")
    elif meta.get("manually_reviewed") is not True:
        result.update(reason="not_manually_reviewed", manual_review_required=True)
        result["uncertainty"].append("source_not_manually_reviewed")
    if not isinstance(raw, list) or any(not isinstance(label, str) or not label for label in raw):
        result.update(reason=result["reason"] or "malformed_or_missing_labels", manual_review_required=True)
        result["uncertainty"].append("malformed_or_missing_labels")
        return result
    labels = set(raw)
    positive = labels & set(label_map["positive_labels"])
    negative = labels & set(label_map["negative_labels"])
    unknown = labels - set(label_map["positive_labels"]) - set(label_map["negative_labels"]) - set(label_map["excluded_labels"])
    if unknown:
        result["manual_review_required"] = True
        result["uncertainty"].append("unknown_labels:" + canonical(sorted(unknown)))
    if positive and negative:
        result["manual_review_required"] = True
        result["uncertainty"].append("positive_and_normal_labels")
    if result["reason"]:
        return result
    if positive:
        result["label"] = 1
    elif labels and labels <= set(label_map["negative_labels"]):
        result["label"] = 0
    else:
        result["reason"] = "empty_labels" if not labels else "unknown_labels" if unknown else "excluded_or_mixed_labels"
        result["manual_review_required"] = bool(unknown) or not labels
    return result


def message(value: Any, node_metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConversionError("malformed_message")
    raw_flags = value.get("metadata")
    if raw_flags is not None and not isinstance(raw_flags, Mapping):
        raise ConversionError("malformed_message_metadata")
    if node_metadata is not None and not isinstance(node_metadata, Mapping):
        raise ConversionError("malformed_node_metadata")
    flags = dict(raw_flags or {})
    if node_metadata:
        flags.update(node_metadata)
    role = value.get("role")
    if flags.get("function_call_result") is True or role in {"function", "tool", "environment"}:
        role = "tool"
    elif role == "assistant":
        role = "agent"
    elif role == "developer":
        role = "system"
    if role not in {"agent", "user", "system", "tool"}:
        raise ConversionError(f"unsupported_message_role:{role}")
    content = value.get("content")
    if content is not None and not isinstance(content, str):
        raise ConversionError("non_text_message_content")
    calls = []
    call = value.get("function_call")
    if call is not None:
        if not isinstance(call, Mapping):
            raise ConversionError("malformed_function_call")
        # Arrow represents absent structs as either null or all-null fields.
        name, arguments = call.get("name"), call.get("arguments")
        if name is not None or arguments is not None:
            if not isinstance(name, str) or not name or not isinstance(arguments, (str, dict, list, type(None))):
                raise ConversionError("malformed_function_call")
            if role != "agent":
                raise ConversionError("function_call_on_non_assistant")
            calls.append({"name": name, "arguments": arguments})
    result = {"source": role, "message": None if role == "tool" else content,
              "tool_calls": calls, "observation": content if role == "tool" else None}
    # Completion type is an execution-format hint, never a label or verdict.
    completion = flags.get("completion_type")
    if role == "agent" and completion in {"reasoning", "analysis", "chain_of_thought"}:
        result["message"] = None
        result["reasoning_content"] = content
    return result


def prefix(left: list[str], right: list[str]) -> bool:
    return len(left) <= len(right) and left == right[:len(left)]


def sample_steps(row: Mapping[str, Any], evidence: Mapping[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    samples = row.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ConversionError("missing_samples")
    if evidence is None:
        raise ConversionError("missing_native_graph_for_sample_validation")
    histories = []
    messages: dict[str, dict[str, Any]] = {}
    for sample in samples:
        if not isinstance(sample, Mapping) or not isinstance(sample.get("input"), list) or not sample["input"]:
            raise ConversionError("malformed_sample_input")
        sample_meta = sample.get("metadata") or {}
        if not isinstance(sample_meta, Mapping):
            raise ConversionError("malformed_sample_metadata")
        incoming = []
        for raw in sample["input"]:
            step = message(raw)
            flags = raw.get("metadata") or {}
            node_id = flags.get("node_id")
            if node_id is None or isinstance(node_id, bool) or not isinstance(node_id, int):
                raise ConversionError("missing_sample_node_identity")
            key = canonical(node_id)
            if key in messages and messages[key] != step:
                raise ConversionError("conflicting_sample_node_content")
            messages[key] = step
            if evidence is not None:
                native = evidence["graph"].get(key)
                if native is None or native[1] != step:
                    raise ConversionError("sample_graph_node_content_conflict")
                parent = flags.get("parent_node_id")
                parent_key = None if parent is None or parent == -1 else canonical(parent)
                if native[0] != parent_key:
                    raise ConversionError("sample_graph_parent_conflict")
            incoming.append(key)
        if len(set(incoming)) != len(incoming):
            raise ConversionError("repeated_node_within_sample_input")
        output = sample.get("output") or []
        if not isinstance(output, list) or any(not isinstance(branch, list) for branch in output):
            raise ConversionError("malformed_output_branches")
        branches = [[message(item) for item in branch] for branch in output]
        if any(step["source"] != "agent" for branch in branches for step in branch):
            raise ConversionError("non_assistant_output_branch")
        actions = [key for key in incoming if messages[key]["source"] in {"agent", "tool"}]
        histories.append((incoming, actions, branches, sample_meta))
    # Execution evidence is the assistant/tool history actually supplied to later
    # requests, not a generated alternative. Context-only budget messages can
    # disappear from later requests without erasing their earlier appearance.
    maximal_actions = max((item[1] for item in histories), key=len)
    if any(not prefix(item[1], maximal_actions) for item in histories):
        raise ConversionError("competing_sample_input_action_branches")
    ordered: list[str] = []
    positions: dict[str, int] = {}
    action_frontier: list[str] = []
    for incoming, actions, _branches, _meta in histories:
        if len(actions) < len(action_frontier) and any(key not in positions for key in incoming):
            raise ConversionError("new_context_on_earlier_sample_branch")
        previous = -1
        for key in incoming:
            if key not in positions:
                positions[key] = len(ordered)
                ordered.append(key)
            if positions[key] <= previous:
                raise ConversionError("incompatible_sample_context_order")
            previous = positions[key]
        if len(actions) >= len(action_frontier):
            action_frontier = actions
    terminal_inputs = [item for item in histories if item[0][-1] == ordered[-1] and item[1] == maximal_actions]
    if not terminal_inputs:
        raise ConversionError("unestablished_terminal_sample")
    candidates = {}
    for incoming, _actions, branches, flags in terminal_inputs:
        if flags.get("unmatched") is True:
            continue
        for branch in branches:
            if not branch:
                continue
            if evidence is not None:
                # A generated alternative alone is not execution evidence. Match
                # its complete message sequence to a parent-linked native suffix.
                frontier = [(key, []) for key in incoming]
                for expected in branch:
                    following = []
                    for parent, path in frontier:
                        for child in evidence["children"].get(parent, []):
                            if child not in positions and evidence["graph"][child][1] == expected:
                                following.append((child, path + [child]))
                    frontier = following
                if not frontier:
                    continue
                for last, path in frontier:
                    if evidence["children"].get(last):
                        continue
                    candidates[canonical(branch)] = (branch, path)
            else:
                candidates[canonical(branch)] = (branch, [])
    terminal_selection = "native_graph_and_sample_agree"
    if len(candidates) != 1:
        # METR can mark a completion unmatched after history rewriting. The
        # native graph can still establish the exact unique executed suffix.
        try:
            _graph_steps, graph_conversion = node_steps({}, evidence)
        except ConversionError:
            raise ConversionError("unestablished_or_ambiguous_terminal_output") from None
        graph_actions = [canonical(key) for key in graph_conversion["selected_node_ids"]
                         if evidence["graph"][canonical(key)][1] is not None
                         and evidence["graph"][canonical(key)][1]["source"] in {"agent", "tool"}]
        observed_actions = [key for key in ordered if messages[key]["source"] in {"agent", "tool"}]
        if not prefix(observed_actions, graph_actions):
            raise ConversionError("sample_graph_action_history_conflict")
        terminal_nodes = graph_actions[len(observed_actions):]
        terminal = [evidence["graph"][key][1] for key in terminal_nodes]
        if not terminal or any(step["source"] != "agent" for step in terminal):
            raise ConversionError("unestablished_or_ambiguous_terminal_output")
        terminal_selection = "unique_action_complete_native_graph_suffix"
    else:
        terminal, terminal_nodes = next(iter(candidates.values()))
    steps = [messages[key] for key in ordered] + terminal
    if not any(step["source"] == "agent" for step in steps):
        raise ConversionError("no_assistant_messages")
    input_actions = {key for key in ordered if messages[key]["source"] in {"agent", "tool"}}
    selected = set(ordered) | set(terminal_nodes)
    omitted = set(evidence["graph"]) - selected if evidence is not None else set()
    omitted_context = [key for key in omitted if evidence["graph"][key][1] is not None and evidence["graph"][key][1]["source"] not in {"agent", "tool"}]
    if omitted_context:
        raise ConversionError("graph_context_absent_from_samples")
    # An omitted output must be an alternative at an observed action fork.
    # A collection of unrelated requests is not one cumulative trajectory.
    for key in omitted:
        parent = evidence["graph"][key][0]
        if parent in omitted:
            continue
        observed_siblings = [child for child in evidence["children"].get(parent, [])
                             if child in selected and evidence["graph"][child][1] is not None
                             and evidence["graph"][child][1]["source"] in {"agent", "tool"}]
        if not observed_siblings:
            raise ConversionError("unaccounted_graph_action_branch")
    return steps, {
        "representation": "complete_context_samples",
        "samples": len(samples),
        "selection": "compatible_observed_input_action_histories_and_unique_native_terminal_suffix",
        "context_complete": True,
        "input_node_ids": [json.loads(key) for key in ordered],
        "terminal_node_ids": [json.loads(key) for key in terminal_nodes],
        "request_input_node_ids": [[json.loads(key) for key in item[0]] for item in histories],
        "input_action_nodes": len(input_actions),
        "terminal_selection": terminal_selection,
        "unmatched_samples": sum(item[3].get("unmatched") is True for item in histories),
        "sample_output_branches": sum(len(item[2]) for item in histories),
        "sample_output_branches_appended": int(terminal_selection == "native_graph_and_sample_agree"),
        "graph_nodes_outside_observed_trajectory": [json.loads(key) for key in sorted(omitted)],
        "graph_validation": evidence["validation"] if evidence is not None else None,
    }


def graph_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    nodes = row.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ConversionError("missing_nodes")
    graph: dict[str, tuple[str | None, dict[str, Any] | None]] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or node.get("node_id") is None:
            raise ConversionError("malformed_node")
        key = canonical(node["node_id"])
        parent = node.get("parent_node_id", node.get("parent"))
        parent_key = None if parent is None or parent == -1 else canonical(parent)
        data = node.get("node_data")
        if not isinstance(data, Mapping):
            raise ConversionError("malformed_node_data")
        raw_message = data.get("message")
        step = message(raw_message, data.get("metadata")) if raw_message is not None else None
        pair = parent_key, step
        if key in graph and graph[key] != pair:
            raise ConversionError("conflicting_duplicate_node_id")
        graph[key] = pair
    children: dict[str, list[str]] = defaultdict(list)
    roots = []
    for key, (parent, _) in graph.items():
        if parent is None:
            roots.append(key)
        elif parent not in graph:
            raise ConversionError("missing_parent_node")
        else:
            children[parent].append(key)
    order = []
    visited = set()
    stack = list(roots)
    while stack:
        key = stack.pop()
        if key in visited:
            raise ConversionError("cyclic_node_graph")
        visited.add(key)
        order.append(key)
        stack.extend(children.get(key, []))
    if len(visited) != len(graph):
        raise ConversionError("disconnected_or_cyclic_node_graph")
    return {"graph": graph, "children": dict(children), "roots": roots, "order": order,
            "validation": {"integrity": "valid_parent_linked_forest", "nodes": len(graph), "roots": len(roots)}}


def node_steps(row: Mapping[str, Any], evidence: Mapping[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence = evidence if evidence is not None else graph_evidence(row)
    graph, children, roots, order = (evidence[key] for key in ("graph", "children", "roots", "order"))
    if len(roots) != 1:
        raise ConversionError("ambiguous_or_cyclic_graph_roots")
    action_counts = {}
    for key in reversed(order):
        step = graph[key][1]
        own_action = step is not None and step["source"] in {"agent", "tool"}
        action_counts[key] = int(own_action) + sum(action_counts[child] for child in children.get(key, []))
    steps = []
    selected = set()
    selected_order = []
    key: str | None = roots[0]
    while key is not None:
        selected.add(key)
        selected_order.append(key)
        step = graph[key][1]
        if step is not None:
            steps.append(step)
        descendants = children.get(key, [])
        if len(descendants) > 1:
            action_branches = [child for child in descendants if action_counts[child]]
            if len(action_branches) != 1:
                raise ConversionError("ambiguous_native_node_branches")
            key = action_branches[0]
        else:
            key = descendants[0] if descendants else None
    if not any(step["source"] == "agent" for step in steps):
        raise ConversionError("no_assistant_messages")
    omitted = sorted(set(graph) - selected)
    return steps, {
        "representation": "native_nodes",
        "nodes": len(graph),
        "selection": "unique_path_containing_all_assistant_and_tool_nodes",
        "context_complete": not omitted,
        "selected_node_ids": [json.loads(key) for key in selected_order],
        "omitted_context_node_ids": [json.loads(key) for key in omitted],
        "assistant_or_tool_nodes_omitted": 0,
    }


def final_state(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    # Deliberately do not use metadata, grades, scores, labels or judge fields.
    final = row.get("final")
    if not isinstance(final, Mapping):
        return None, None
    reward, diff = final.get("reward"), final.get("diff")
    if reward is not None:
        if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward):
            raise ConversionError("malformed_execution_reward")
        reward = float(reward)
    if diff is not None and not isinstance(diff, str):
        raise ConversionError("malformed_final_diff")
    return reward, diff


def group_id(source: Source, run_source: str) -> str:
    # Hash preserves distinct source groups even when sanitization collides.
    group = re.sub(r"[^A-Za-z0-9._-]", "_", run_source)[:48]
    return f"{REPOSITORIES[source.repo]}--{source.split}--{group}-{hashlib.sha256(run_source.encode()).hexdigest()[:8]}"


def new_counts() -> dict[str, Any]:
    return {"raw_rows": 0, "raw_labels": Counter(), "raw_label_sets": Counter(), "review_state": Counter(),
            "mapped_labels": Counter(), "exclusions": Counter(), "emitted_labels": Counter(), "clusters": Counter(),
            "models": Counter(), "chain_of_thought": Counter(), "emitted_chain_of_thought": Counter(),
            "emitted_context": Counter()}


def annotation(meta: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: meta.get(key, {"missing": True})
              for key in ("labels", "manually_reviewed", "model", "public", "has_chain_of_thought")}
    raw = result["labels"]
    if isinstance(raw, list) and all(isinstance(label, str) for label in raw):
        result["labels"] = sorted(set(raw))
    return result


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def convert(manifest_path: Path, output: Path, label_path: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    if any((parent / ".git").exists() for parent in [output, *output.parents]):
        raise ValueError("Output must be outside every Git checkout")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Output must be absent or an empty directory; existing evidence is never overwritten")
    label_map = load_label_map(label_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources, skipped = sources_from_manifest(manifest, manifest_path.resolve().parent)
    helpers = load_helpers()
    groups: dict[str, dict[str, Any]] = defaultdict(new_counts)
    records: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    invalid = []
    counts_by_file = []
    for file_index, source in enumerate(sources):
        count = 0
        for ordinal, row in enumerate(rows(source, metadata_only=True)):
            count += 1
            meta = metadata(row)
            group = group_id(source, str(meta.get("run_source", "unknown")))
            stats = groups[group]
            stats["raw_rows"] += 1
            raw = meta.get("labels")
            stats["raw_label_sets"][canonical(raw)] += 1
            if isinstance(raw, list):
                stats["raw_labels"].update(label if isinstance(label, str) else "<malformed>" for label in raw)
            stats["review_state"][canonical(meta.get("manually_reviewed"))] += 1
            stats["models"][canonical(meta.get("model"))] += 1
            stats["chain_of_thought"][canonical(meta.get("has_chain_of_thought"))] += 1
            decision = classify(meta, label_map)
            if decision["label"] is not None:
                stats["mapped_labels"][str(decision["label"])] += 1
            else:
                stats["exclusions"][decision["reason"]] += 1
            try:
                key = identity(meta)
            except ConversionError as error:
                invalid.append({"file_index": file_index, "row": ordinal, "reason": str(error)})
                stats["exclusions"][str(error)] += 1
                continue
            records[key].append({"file_index": file_index, "row": ordinal, "group": group,
                                 "meta": meta, "decision": decision})
        if source.expected_rows is not None and count != source.expected_rows:
            raise ValueError(f"Source row count mismatch: {source.path}")
        counts_by_file.append({**source.receipt(), "rows": count})
    conflicts = set()
    eligible = set()
    relations = []
    for key, variants in sorted(records.items()):
        annotations = [annotation(variant["meta"]) for variant in variants]
        signatures = {canonical(value) for value in annotations}
        conflicting_fields = [field for field in annotations[0] if len({canonical(value[field]) for value in annotations}) > 1]
        if len(signatures) > 1:
            conflicts.add(key)
        elif variants[0]["decision"]["label"] is not None:
            eligible.add(key)
        if len(variants) > 1:
            relations.append({"identity": list(key), "relation": "metadata_conflict" if key in conflicts else "same_native_identity",
                              "conflicting_fields": conflicting_fields,
                              "occurrences": [{"file_index": value["file_index"], "row": value["row"], "set": value["group"],
                                               "annotation": annotation(value["meta"])} for value in variants],
                              "transcript_equivalence": "checked_by_native_input_node_identity_when_available"})
    output.mkdir(parents=True, exist_ok=True)
    failures = []
    emitted: dict[tuple[str, str, str], str] = {}
    target_rows = {(variant["file_index"], variant["row"]): key for key in eligible for variant in records[key]}
    # Validate every eligible graph even when the sample representation wins.
    # A forest can be intact while the graph-only linearization remains unusable.
    graph_by_identity = {}
    graph_results = {}
    graph_audits = []
    graph_invalid = set()
    for file_index, source in enumerate(sources):
        if not source.repo.endswith("/malt-transcripts-public"):
            continue
        wanted = {ordinal for (index, ordinal) in target_rows if index == file_index}
        if not wanted:
            continue
        for ordinal, row in enumerate(rows(source)):
            if ordinal not in wanted:
                continue
            location = file_index, ordinal
            key = target_rows[location]
            audit = {"identity": list(key), "file_index": file_index, "row": ordinal}
            try:
                evidence = graph_evidence(row)
                audit.update(evidence["validation"])
                if key in graph_by_identity and graph_by_identity[key]["graph"] != evidence["graph"]:
                    raise ConversionError("conflicting_duplicate_native_graph")
                graph_by_identity[key] = evidence
            except ConversionError as error:
                audit.update(integrity="failed", integrity_failure=str(error))
                graph_invalid.add(key)
            else:
                try:
                    graph_results[location] = node_steps(row, evidence)
                    audit["graph_only_conversion"] = "usable"
                except ConversionError as error:
                    audit.update(graph_only_conversion="failed", graph_only_failure=str(error))
                    failures.append({**audit, "set": group_id(source, key[0]), "reason": str(error),
                                     "manual_review_required": True})
            graph_audits.append(audit)
    for key in graph_invalid:
        failures.append({"identity": list(key), "reason": "native_graph_integrity_conflict", "manual_review_required": True})
    audit_by_identity = defaultdict(list)
    for audit in graph_audits:
        audit_by_identity[tuple(audit["identity"])].append(audit)
    cluster_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    context_partial_runs = 0
    manual_review_runs = 0
    emitted_strata: Counter[str] = Counter()
    transcript_conflicts = set()
    with (output / "runs.jsonl").open("x", encoding="utf-8") as run_handle:
        for file_index, source in enumerate(sources):
            wanted = {ordinal for (index, ordinal), key in target_rows.items()
                      if index == file_index and key not in emitted and key not in graph_invalid and key not in transcript_conflicts
                      and (source.repo.endswith("/malt-public") or (index, ordinal) in graph_results)}
            if not wanted:
                continue
            for ordinal, row in enumerate(rows(source)):
                if ordinal not in wanted:
                    continue
                key = target_rows[(file_index, ordinal)]
                if key in emitted or key in transcript_conflicts:
                    continue
                variant = next(value for value in records[key] if value["file_index"] == file_index and value["row"] == ordinal)
                group = variant["group"]
                try:
                    if "samples" in row:
                        steps, conversion = sample_steps(row, graph_by_identity.get(key))
                    elif "nodes" in row:
                        steps, conversion = graph_results[(file_index, ordinal)]
                    else:
                        raise ConversionError("missing_native_transcript")
                    reward, diff = final_state(row)
                    native = canonical(list(key))
                    run_key = f"{group}:{native}"
                    view = helpers.build_view(run_key, steps, reward=reward, diff=diff)
                    full, actions = helpers.write_views(group, native, view, root=output / "views")
                    n_steps, n_chars = helpers.view_size(view)
                    decision = variant["decision"]
                    conversion_uncertainty = [] if conversion.get("context_complete", True) else ["non_action_context_branches_omitted"]
                    conversion["graph_audits"] = audit_by_identity.get(key, [])
                    context_status = "partial" if conversion_uncertainty else "complete"
                    cluster = f"malt:{key[2]}"
                    extras = {"source_identity": list(key), "source_corpus": "metr-evals/malt-public",
                              "source_repo": source.repo, "source_revision": source.revision,
                              "source_file": source.relative, "source_sha256": source.digest, "source_row": ordinal,
                              "native_split": source.split, "run_source": key[0], "task_id": key[2], "run_id": key[1],
                              "raw_labels": decision["raw_labels"], "manually_reviewed": decision["manually_reviewed"],
                              "model": variant["meta"].get("model"), "public": variant["meta"].get("public"),
                              "has_chain_of_thought": variant["meta"].get("has_chain_of_thought"),
                              "context_complete": not conversion_uncertainty,
                              "mapping_reason": "explicit_positive_label" if decision["label"] else "explicit_normal_only",
                              "read_by_status": "source-level prior read: 572 redistributed MALT runs; detector-specific prior use unverified",
                              "manual_review_required": decision["manual_review_required"] or bool(conversion_uncertainty),
                              "uncertainty": decision["uncertainty"] + conversion_uncertainty,
                              "label_semantics": "attempted_behavior_not_proven_success", "label_map_version": label_map["version"],
                              "variant_count": len(records[key]), "conversion": conversion}
                    result = helpers.runs_row(set_id=group, native_id=native, split="heldout", label=decision["label"],
                                              label_type="human", natural=True, cluster=cluster, source_family="malt",
                                              view_full=full, view_actions=actions, n_steps=n_steps, n_chars=n_chars,
                                              read_by=["failure-library:agent-text-regex/v1 (source-level partial read)"], extra=extras)
                    run_handle.write(canonical(result) + "\n")
                    emitted[key] = run_key
                    label_counts[str(decision["label"])] += 1
                    context_partial_runs += int(bool(conversion_uncertainty))
                    manual_review_runs += int(result["manual_review_required"])
                    emitted_strata[canonical({"source_repo": source.repo, "run_source": key[0],
                                              "has_chain_of_thought": variant["meta"].get("has_chain_of_thought"),
                                              "context": context_status, "label": decision["label"]})] += 1
                    cluster_counts[cluster] += 1
                    groups[group]["emitted_labels"][str(decision["label"])] += 1
                    groups[group]["clusters"][cluster] += 1
                    groups[group]["emitted_chain_of_thought"][canonical(variant["meta"].get("has_chain_of_thought"))] += 1
                    groups[group]["emitted_context"][context_status] += 1
                except ConversionError as error:
                    if str(error) in {"sample_graph_node_content_conflict", "sample_graph_parent_conflict",
                                      "conflicting_sample_node_content", "sample_graph_action_history_conflict"}:
                        transcript_conflicts.add(key)
                    failures.append({"identity": list(key), "file_index": file_index, "row": ordinal,
                                     "set": group, "reason": str(error), "manual_review_required": True})
    for relation in relations:
        key = tuple(relation["identity"])
        relation["emitted_run_key"] = emitted.get(key)
        relation["counted_runs"] = int(key in emitted)
    exclusion_rows = []
    for key, variants in sorted(records.items()):
        if key in emitted:
            continue
        decision = variants[0]["decision"]
        reason = ("variant_metadata_conflict" if key in conflicts else "native_graph_integrity_conflict" if key in graph_invalid
                  else "variant_transcript_conflict" if key in transcript_conflicts
                  else decision["reason"] or "all_representations_failed_conversion")
        exclusion_rows.append({"identity": list(key), "reason": reason, "raw_labels": decision["raw_labels"],
                               "manually_reviewed": decision["manually_reviewed"], "manual_review_required": key in conflicts or key in eligible or decision["manual_review_required"],
                               "uncertainty": decision["uncertainty"], "occurrences": len(variants)})
    write_json(output / "variant-relations.json", relations)
    write_json(output / "exclusions.json", exclusion_rows)
    write_json(output / "conversion-failures.json", failures)
    write_json(output / "graph-audits.json", graph_audits)
    coverage = {"version": "malt-detbench-adapter-v2", "source_manifest": str(manifest_path.resolve()),
                "source_manifest_sha256": sha256(manifest_path), "label_map_version": label_map["version"],
                "label_map_sha256": sha256(label_path), "adapter_sha256": sha256(Path(__file__)),
                "files": counts_by_file, "skipped_files": skipped, "manifest_skipped_files": manifest.get("skipped_files", []), "groups": dict(groups),
                "raw_rows": sum(item["rows"] for item in counts_by_file), "unique_native_identities": len(records),
                "duplicate_occurrences": sum(len(value) - 1 for value in records.values()),
                "overlapping_identities": len(relations), "metadata_conflicts": len(conflicts),
                "invalid_identity_rows": invalid, "mapped_unique_labels": dict(Counter(str(records[key][0]["decision"]["label"]) for key in eligible)),
                "emitted_runs": len(emitted), "emitted_labels": dict(label_counts),
                "context_complete_runs": len(emitted) - context_partial_runs,
                "context_partial_runs": context_partial_runs, "manual_review_required_runs": manual_review_runs,
                "emitted_strata": [{"stratum": json.loads(key), "runs": count} for key, count in sorted(emitted_strata.items())],
                "graph_audited_identities": len(audit_by_identity),
                "graph_integrity_failed_identities": len(graph_invalid),
                "quarantined_transcript_conflicts": len(transcript_conflicts),
                "graph_only_failure_reasons": dict(Counter(audit["graph_only_failure"] for audit in graph_audits if "graph_only_failure" in audit)),
                "excluded_unique_identities": len(exclusion_rows), "exclusion_reasons": dict(Counter(item["reason"] for item in exclusion_rows)),
                "conversion_failure_attempts": len(failures), "conversion_failed_identities": len(eligible - emitted.keys()),
                "conversion_failed_labels": dict(Counter(str(records[key][0]["decision"]["label"]) for key in eligible - emitted.keys())),
                "conversion_failure_reasons": dict(Counter(item["reason"] for item in failures)),
                "cluster_count": len(cluster_counts), "clusters": dict(cluster_counts), "label_policies": label_map["policies"],
                "deduplication": "One run per (run_source, run_id, task_id); prefer malt-public, then equivalent-metadata transcript representation; quarantine metadata conflicts.",
                "limitations": ["Labels mark attempted behavior, not proven successful cheating.",
                                "Only explicitly manually reviewed natural rows enter the benchmark.",
                                "Competing observed input action histories and unresolved final alternatives fail conversion rather than flattening.",
                                "Unique action-complete graph paths can omit context-only side branches; these runs require manual review.",
                                "Same identity does not prove byte-identical or semantically identical transcripts.",
                                "Unavailable execution reward/diff remain null; no label-derived final state."]}
    write_json(output / "coverage.json", coverage)
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    coverage = convert(args.source_manifest, args.output, Path(__file__).with_name("label-map.json"))
    print(canonical({key: coverage[key] for key in ("raw_rows", "unique_native_identities", "emitted_runs", "emitted_labels", "conversion_failed_identities", "cluster_count")}))


if __name__ == "__main__":
    main()
