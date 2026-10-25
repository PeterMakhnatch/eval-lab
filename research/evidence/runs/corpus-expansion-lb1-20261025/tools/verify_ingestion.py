#!/usr/bin/env python3
"""Verify only LB-1 raw imports and the preexisting explicitly-DEV cohort."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

os.environ['EVALLAB_DISABLE_LOCAL_DOCKER'] = '1'
ROOT = Path('/private/tmp/lb1-20261025')
LIVE = Path('/Users/petermakhnatch/.local/state/evallab-prod-live')
MODULE = ROOT / 'cloud_trace_db-822d9c49e.py'
assert hashlib.sha256(MODULE.read_bytes()).hexdigest() == '8233cdad2d080059e4d1bfc41d74cb9f89032c0fefa0217847c87d8230e86a59'
spec = importlib.util.spec_from_file_location('lb1_cloud_trace_snapshot_verify', MODULE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def main():
    report = {'schema':'lb1.reopened_ingestion_verification/v1',
              'created_at':datetime.now(UTC).isoformat(), 'database':str(LIVE/'cloud-trace-db.sqlite'),
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'protected_registry_reads':0, 'benchmark_executions':0, 'queue_dispatches':0,
              'runtime_admissions':0, 'cloud_connections':0, 'docker_runs':0}
    baseline = json.loads((ROOT/'preexisting-dev-row-fingerprints.private.json').read_text())
    with sqlite3.connect((LIVE/'cloud-trace-db.sqlite').as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        report['integrity_check']=connection.execute('PRAGMA integrity_check').fetchone()[0]
        assert report['integrity_check']=='ok'
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
        report['foreign_key_check']='ok'
        report['preexisting_rows']={}
        pks={'corpus_tasks':('task_id',),'agent_traces':('trace_id',),'task_issues':('issue_id',),'trace_steps':('trace_id','step_index')}
        for table, old in baseline['tables'].items():
            checked=0
            for key,expected in old['row_hashes'].items():
                values=json.loads(key)
                where=' AND '.join(column+'=?' for column in pks[table])
                row=connection.execute('SELECT * FROM '+table+' WHERE '+where,values).fetchone()
                assert row is not None, ('preexisting row missing',table)
                assert hashlib.sha256(canonical(dict(row)).encode()).hexdigest()==expected, ('preexisting row changed',table)
                checked+=1
            report['preexisting_rows'][table]={'unchanged':checked,'baseline_sha256':old['aggregate_sha256']}
        report['table_counts']={table:connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in pks}
        report['metadata_datasets']=[dict(row) for row in connection.execute("SELECT dataset_id,split,COUNT(*) AS rows,SUM(instruction_text='') AS empty_instructions FROM corpus_tasks WHERE dataset_id IN ('SimGlot','openai-swe-bench-annotations-2024','opencompass-swebench-pro-verified-correction-metadata') GROUP BY dataset_id,split")]
        assert all(row['split']=='held' and row['rows']==row['empty_instructions'] for row in report['metadata_datasets'])
        for table in ('agent_traces','task_issues'):
            count=connection.execute('SELECT COUNT(*) FROM '+table+" a JOIN corpus_tasks c ON c.task_id=a.task_id WHERE c.dataset_id IN ('SimGlot','openai-swe-bench-annotations-2024','opencompass-swebench-pro-verified-correction-metadata')").fetchone()[0]
            assert count==0, 'metadata source gained fabricated traces/issues'
        report['metadata_sources_have_no_issue_or_trace_rows']=True
    cas=module.S3CompatibleCAS(local_directory=LIVE/'cas',bucket='')
    with module.CloudTraceDatabase(LIVE/'cloud-trace-db.sqlite') as database:
        catalog=database.get_task('simglot:catalog-metadata:ded6d3e144d6af89a54ad7604b17d499b0ca2147')
        assert catalog and catalog['split']=='held' and catalog['instruction_text']==''
        assert catalog['metadata_json']['runnable'] is False
        uri=catalog['metadata_json']['scorer_only']['metadata_jsonl_cas_uri']
        data=cas.get_bytes(uri)
        envelopes=[json.loads(line) for line in data.splitlines() if line]
        assert len(envelopes)==840
        assert len({row['record_id'] for row in envelopes})==840
        kinds=Counter(row['record_kind'] for row in envelopes)
        assert kinds==Counter({'oracle_detector_observation':192,'extended_detector_observation':528,'variant_target_validity':63,'mutant_catalog_entry':21,'appworld_applicability':8,'aggregate_cell':16,'archive_manifest_entry':4,'deposit_file_metadata':7,'availability_snapshot':1})
        groups=Counter(row['scorer_only']['duplicate_group_id'] for row in envelopes if row['record_kind'] in ('oracle_detector_observation','extended_detector_observation'))
        assert len(groups)==528 and Counter(groups.values())==Counter({2:192,1:336})
        report['simglot']={'raw_jsonl_sha256':hashlib.sha256(data).hexdigest(),'envelopes':len(envelopes),'unique_record_ids':840,'distinct_detector_groups':528,'duplicated_original_observations':192,'record_kinds':dict(kinds),'catalog_rows':1,'runnable_tasks':0,'trace_steps':0}
        ratings=cas.get_bytes('cas://sha256/c33da21f1a511f919a45a9408353c8ff7865f14dd891b76c02c076db790b3ace')
        rating_count=sum(1 for line in ratings.splitlines() if line)
        assert rating_count==5097
        report['swe_native_ratings_retained']=rating_count
        prolabels=Counter()
        verified_members=0
        checked=0
        for name in ('corpus_tasks.swe-annotations.jsonl','corpus_tasks.swebench-pro-verified.jsonl'):
            with (ROOT/'swe'/'out'/'handoff'/name).open() as stream:
                for line in stream:
                    task=json.loads(line)
                    actual=database.get_task(task['task_id'])
                    expected={'original_grader_digest':None,'repaired_grader_digest':None,'cas_uri':None,'metadata_json':{},**task}
                    assert actual==expected
                    cas.get_bytes(actual['cas_uri'])
                    checked+=1
                    metadata=actual['metadata_json']
                    if 'correction_label' in metadata:
                        label=metadata['correction_label']
                        assert label['clean_verdict'] is None
                        prolabels[label.get('readme_category') or 'unlabelled']+=1
                    else:
                        verified_members+=bool(metadata['task_membership']['in_swe_bench_verified_500'])
        assert checked==2430 and verified_members==500
        assert prolabels==Counter({'misleading_prompt':22,'narrow_test':75,'broad_test':3,'other':2,'unlabelled':629})
        report['swe_pro_exact_reopened_task_roundtrips']=checked
        report['verified_subset_members']=verified_members
        report['pro_source_labels']=dict(prolabels)
    destination=LIVE/'corpus-expansion-lb1'/'reopened-verification.json'
    with destination.open('x') as stream:
        json.dump(report,stream,indent=2,sort_keys=True)
        stream.write('\n')
    print(json.dumps(report,indent=2,sort_keys=True))


if __name__=='__main__':
    main()
