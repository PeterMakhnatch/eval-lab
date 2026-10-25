#!/usr/bin/env python3
"""Complete only pre-cleared source families; private local raw evidence, no admission."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
ROOT = Path('/private/tmp/lb1-20261025')
WORKER = ROOT / 'goodfire-wrench'
PRODUCTION = Path('/Users/petermakhnatch/.local/state/evallab-prod-live')
OUT = ROOT / 'parent-safe-traces'
SNAPSHOT = ROOT / 'cloud_trace_db-822d9c49e.py'
SNAPSHOT_SHA = '8233cdad2d080059e4d1bfc41d74cb9f89032c0fefa0217847c87d8230e86a59'
NORMALIZER = WORKER / 'tools/goodfire_normalize_v3.py'
SAFE_RUNS = (
    'swebench-verified-kimi-k3-nohack',
    'swebench-verified-glm-5.2-nohack',
    'swebench-verified-qwen3.8-2.4t-a95b-fp8-nohack',
    'impossiblebench-glm-5.2-fp8',
    'impossiblebench-kimi-k3',
    'impossiblebench-qwen3.8-2.4t-a95b-fp8',
)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


assert sha(SNAPSHOT) == SNAPSHOT_SHA
module = load_module('lb1_frozen_trace_module', SNAPSHOT)
sys.modules['evallab.production.cloud_trace_db'] = module
# Read-only reuse of model-authored normalization functions; no source rewriting.
normalizer = load_module('lb1_goodfire_model_normalizer', NORMALIZER)


class LocalCAS(module.S3CompatibleCAS):
    def __init__(self, directory):
        super().__init__(local_directory=directory, bucket='')
        self.objects = {}

    def put_bytes(self, data, **kwargs):
        uri = super().put_bytes(data, **kwargs)
        if uri not in self.objects:
            assert self.get_bytes(uri) == data
            self.objects[uri] = len(data)
        return uri


class NewSourceWriter:
    def __init__(self, database):
        self.db = database
        self.tasks = set()
        self.traces = set()
        self.steps = 0
        self.native_nodes_reconstructed = 0

    def upsert_task(self, task):
        assert task['split'] == 'held'
        assert task['dataset_id'] in ('goodfire-reward-hack-data', 'terminal-wrench')
        identity = task['task_id']
        if identity not in self.tasks:
            assert self.db.get_task(identity) is None, 'refusing to replace a preexisting task'
            self.tasks.add(identity)
        self.db.upsert_task(task)
        assert self.db.get_task(identity) == task

    def upsert_trace(self, trace, *, steps):
        identity = trace['trace_id']
        assert identity not in self.traces
        assert self.db.get_trace(identity) is None, 'refusing to replace a preexisting trace'
        assert trace['task_id'] in self.tasks
        assert trace['model_id'].startswith('source-reported:')
        assert trace['verified_receipt_json']['status'] == 'provisional'
        assert trace['is_exploit'] is False and trace['is_task_defect_fail'] is False
        assert trace['step_count'] == len(steps)
        self.db.upsert_trace(trace, steps=steps)
        back = self.db.get_trace(identity)
        assert {k: back[k] for k in trace} == trace
        assert back['steps'] == steps
        assert back['issues'] == []
        raw = cas.get_bytes(trace['raw_cas_uri'])
        native = json.loads(raw)
        if trace['task_id'].startswith('goodfire-reward-hack-data/'):
            nodes = native['traces'][0]['nodes']
            assert len(nodes) == len(back['steps'])
            for built, node in zip(back['steps'], nodes, strict=True):
                assert normalizer.reconstruct(built) == node
                self.native_nodes_reconstructed += 1
        else:
            nodes = native['steps']
            assert len(nodes) == len(back['steps'])
            for built, node in zip(back['steps'], nodes, strict=True):
                flags = built['flags_json']
                rebuilt = copy.deepcopy(flags['native_step_residual'])
                if flags['message_key_present']:
                    rebuilt['message'] = (json.loads(built['content_text'])
                        if flags['content_encoding'] == 'canonical_json' else built['content_text'])
                assert rebuilt == node
                if built['observation_cas_uri']:
                    assert json.loads(cas.get_bytes(built['observation_cas_uri'])) == node['observation']
                self.native_nodes_reconstructed += 1
        self.traces.add(identity)
        self.steps += len(steps)


def copy_uri(uri, origin):
    if uri in cas.objects:
        return
    data = origin.get_bytes(uri)
    assert cas.put_bytes(data) == uri


def referenced_uris(value):
    if isinstance(value, str) and value.startswith('cas://sha256/'):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from referenced_uris(item)
    elif isinstance(value, list):
        for item in value:
            yield from referenced_uris(item)


if __name__ == '__main__':
    OUT.mkdir(exist_ok=False)
    handoff = OUT / 'handoff'
    handoff.mkdir()
    receipt_path = PRODUCTION / 'corpus-expansion-lb1/safe-traces-ingestion.json'
    assert not receipt_path.exists()
    coverage = json.loads((WORKER / 'goodfire/coverage.json').read_text())
    assert coverage['alias_gate_counts'] == {'deepswe:hold': 113, 'impossiblebench:pass_structural': 103, 'swebench_verified:pass_structural': 500}
    fetch = {}
    for name in ('goodfire-fetch-metadata.json', 'goodfire-fetch-streams.json'):
        for row in json.loads((WORKER / name).read_text())['files']:
            fetch[row['path']] = row
    cas = LocalCAS(PRODUCTION / 'cas')
    source_cas = module.S3CompatibleCAS(local_directory=WORKER / 'cas', bucket='')
    runs = []
    with module.CloudTraceDatabase(PRODUCTION / 'cloud-trace-db.sqlite') as db:
        writer = NewSourceWriter(db)
        # Wrench: only the two exact preexisting canonical DEV names; no Parquet scan.
        wr_fetch = json.loads((WORKER / 'wrench-fetch-dev-subset-v3.json').read_text())
        assert wr_fetch['allowlist_sha256'] == 'c82b251f5c5bb51466b154222e3cec3bb7cbd389768ce1e79cf17d55f63dc8fa'
        wr_tasks = list(map(json.loads, (WORKER / 'wrench/handoff-v3/tasks.jsonl').open()))
        assert len(wr_tasks) == 2
        wr_labels = Counter()
        for row in wr_fetch['files']:
            copy_uri(row['cas_uri'], source_cas)
        for task in wr_tasks:
            for uri in referenced_uris(task):
                copy_uri(uri, source_cas)
            writer.upsert_task(task)
        for line in (WORKER / 'wrench/handoff-v3/traces.jsonl').open():
            row = json.loads(line)
            for uri in referenced_uris(row):
                copy_uri(uri, source_cas)
            writer.upsert_trace(row['trace'], steps=row['steps'])
            wr_labels[row['trace']['verified_receipt_json']['source_label']['classification']] += 1
        wr_counts = {'task_rows': len(writer.tasks), 'trace_views': len(writer.traces), 'step_rows': writer.steps,
                     'source_view_labels': dict(wr_labels), 'independent_original_run_lineages': 72,
                     'fetch_receipt_sha256': sha(WORKER / 'wrench-fetch-dev-subset-v3.json'),
                     'normalize_receipt_sha256': sha(WORKER / 'wrench-normalize-receipt-v3.json')}
        print(json.dumps({'terminal_wrench_completed': wr_counts}), flush=True)
        tasks = {}
        for run in SAFE_RUNS:
            result = normalizer.decode_run(run, WORKER / 'goodfire/raw' / normalizer.REVISION,
                                            cas, fetch, OUT, writer, tasks, handoff)
            runs.append(result)
            print(json.dumps({'goodfire_run_completed': {k:v for k,v in result.items() if k not in ('files_sha256','handoff')}}), flush=True)
        for task in tasks.values():
            writer.upsert_task(task)
        receipt = {'schema': 'lb1.safe_trace_raw_ingestion/v1', 'created_at': datetime.now(UTC).isoformat(),
                   'source_commit': '822d9c49e6797cce5f6c3e119bc7d607efb824f3',
                   'database_module_sha256': SNAPSHOT_SHA, 'parent_script_sha256': sha(__file__),
                   'model_authored_normalizer_sha256': sha(NORMALIZER),
                   'production_database': str(PRODUCTION / 'cloud-trace-db.sqlite'),
                   'scope': 'private local raw evidence only; all new task splits held and all source labels provisional',
                   'terminal_wrench': wr_counts, 'goodfire': {'decoded_runs': runs, 'task_rows': len(tasks),
                   'held_runs_not_read_by_parent': [r['run'] for r in coverage['runs'] if r['family']=='deepswe']},
                   'new_task_rows': len(writer.tasks), 'new_trace_rows': len(writer.traces), 'new_step_rows': writer.steps,
                   'exact_trace_readbacks': len(writer.traces), 'exact_native_step_reconstructions': writer.native_nodes_reconstructed,
                   'copied_or_created_cas_objects': len(cas.objects), 'copied_or_created_cas_bytes': sum(cas.objects.values()),
                   'runtime_admissions':0,'registry_registrations':0,'queue_dispatches':0,'docker_runs':0,
                   'cloud_connections':0,'benchmark_executions':0,'issue_rows_created':0,
                   'protected_registry_reads_by_parent':0,
                   'source_io_limit': 'Worker fetched mixed task Parquet and all nine Goodfire streams before final source clearance; this receipt does not certify those child downloads as heldout-free. Parent reads only the six structural-safe Goodfire runs and exact DEV-name-matched Wrench handoffs. Three unresolved Goodfire runs and all other Wrench task bodies are excluded from this integration.'}
    with receipt_path.open('x') as stream:
        json.dump(receipt, stream, indent=2, sort_keys=True)
        stream.write('\n')
    print(json.dumps({'completed_receipt': str(receipt_path), 'new_tasks': receipt['new_task_rows'],
                      'new_traces': receipt['new_trace_rows'], 'new_steps': receipt['new_step_rows']}, sort_keys=True), flush=True)
