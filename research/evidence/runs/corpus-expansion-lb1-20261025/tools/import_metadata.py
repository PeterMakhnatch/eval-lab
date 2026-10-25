#!/usr/bin/env python3
"""LB-1 local raw-evidence intake; never grants DEV/runtime/scorer admission."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from datetime import UTC, datetime
from pathlib import Path

os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
SNAPSHOT = Path('/private/tmp/lb1-20261025/cloud_trace_db-822d9c49e.py')
SNAPSHOT_SHA256 = '8233cdad2d080059e4d1bfc41d74cb9f89032c0fefa0217847c87d8230e86a59'
assert hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() == SNAPSHOT_SHA256
spec = importlib.util.spec_from_file_location('lb1_committed_cloud_trace_db', SNAPSHOT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CloudTraceDatabase, CAS = module.CloudTraceDatabase, module.S3CompatibleCAS


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def rows(path: Path):
    with path.open('rb') as stream:
        for number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError(f'{path.name}:{number}: not an object')
            yield number, raw, record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists():
        raise FileExistsError('receipts are immutable; choose a new receipt path')
    config = json.loads(args.spec.read_text())
    root = Path(config['production_root'])
    database_path = root / 'cloud-trace-db.sqlite'
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    destination = CAS(local_directory=root / 'cas', bucket='')
    report = {
        'schema': 'lb1.raw_metadata_ingestion/v1',
        'created_at': datetime.now(UTC).isoformat(),
        'database': str(database_path),
        'source_commit': '822d9c49e6797cce5f6c3e119bc7d607efb824f3',
        'cloud_trace_db_module_sha256': SNAPSHOT_SHA256,
        'importer_sha256': digest_file(Path(__file__)),
        'spec_sha256': digest_file(args.spec),
        'runtime_admissions': 0, 'queue_events': 0, 'training_exports': 0,
        'trace_rows_created': 0, 'trace_steps_created': 0, 'issue_rows_created': 0,
        'docker_runs': 0, 'cloud_connections': 0,
        'storage_scope': 'private local operator raw evidence only; split held is not an actor filter',
        'sources': [],
    }
    seen = set()
    with CloudTraceDatabase(database_path) as database:
        for source in config['sources']:
            manifest_path = Path(source['cas_manifest'])
            if digest_file(manifest_path) != source['cas_manifest_sha256']:
                raise ValueError('source CAS manifest digest mismatch')
            manifest = json.loads(manifest_path.read_text())
            origin = CAS(local_directory=Path(source['cas_root']), bucket='')
            source_report = {'source': source['source'], 'copied_cas_objects': 0,
                             'copied_cas_bytes': 0, 'input_artifacts': [], 'task_observations': 0,
                             'dataset_counts': {}, 'exact_roundtrips_verified': 0}
            objects = manifest['objects']
            for obj in objects:
                uri = obj['cas_uri']
                data = origin.get_bytes(uri)
                actual = hashlib.sha256(data).hexdigest()
                if uri != 'cas://sha256/' + actual or actual != obj['sha256'] or len(data) != obj['bytes']:
                    raise ValueError('source CAS object failed digest/length attestation')
                copied = destination.put_bytes(data)
                if copied != uri or destination.get_bytes(copied) != data:
                    raise ValueError('destination CAS roundtrip mismatch')
                source_report['copied_cas_objects'] += 1
                source_report['copied_cas_bytes'] += len(data)
            for item in source['task_jsonl']:
                path = Path(item['path'])
                file_sha = digest_file(path)
                if file_sha != item['sha256']:
                    raise ValueError('task handoff digest mismatch')
                count = 0
                for number, raw, task in rows(path):
                    if task['split'] != 'held' or task['instruction_text'] != '':
                        raise ValueError(f'{path.name}:{number}: metadata must remain held with empty instructions')
                    metadata = task['metadata_json']
                    if not isinstance(metadata, dict) or not metadata.get('record_kind'):
                        raise ValueError('metadata observation requires explicit native record_kind')
                    identity = task['task_id']
                    if identity in seen:
                        raise ValueError('duplicate task identity in this import')
                    seen.add(identity)
                    if database.get_task(identity) is not None:
                        raise ValueError('refusing to overwrite any preexisting task row')
                    if task.get('cas_uri'):
                        destination.get_bytes(task['cas_uri'])
                    database.upsert_task(task)
                    stored = database.get_task(identity)
                    expected = {'original_grader_digest': None, 'repaired_grader_digest': None,
                                'cas_uri': None, 'metadata_json': {}, **task}
                    if stored != expected:
                        raise ValueError('complete task metadata roundtrip mismatch')
                    count += 1
                    source_report['exact_roundtrips_verified'] += 1
                    ds = task['dataset_id']
                    source_report['dataset_counts'][ds] = source_report['dataset_counts'].get(ds, 0) + 1
                if count != item['expected_records']:
                    raise ValueError('source row count mismatch')
                source_report['task_observations'] += count
                source_report['input_artifacts'].append({'name': path.name, 'sha256': file_sha, 'records': count})
            for extra in source.get('retain_artifacts', []):
                path = Path(extra['path'])
                if digest_file(path) != extra['sha256']:
                    raise ValueError('retained source artifact digest mismatch')
                uri = destination.put_bytes(path.read_bytes())
                destination.get_bytes(uri)
                source_report['input_artifacts'].append({'name': path.name, 'sha256': extra['sha256'], 'cas_uri': uri})
            report['sources'].append(source_report)
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    with args.receipt.open('x') as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
