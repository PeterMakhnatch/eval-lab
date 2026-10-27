#!/usr/bin/env python3
"""Reopen only explicit LB-1 source rows and original known-DEV fingerprints."""
import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
os.environ['EVALLAB_DISABLE_LOCAL_DOCKER']='1'
root=Path('/private/tmp/lb1-20261025')
prod=Path('/Users/petermakhnatch/.local/state/evallab-prod-live')
source_receipt=json.loads((prod/'corpus-expansion-lb1/safe-traces-ingestion.json').read_text())
snapshot=root/'cloud_trace_db-822d9c49e.py'
assert hashlib.sha256(snapshot.read_bytes()).hexdigest()=='8233cdad2d080059e4d1bfc41d74cb9f89032c0fefa0217847c87d8230e86a59'
spec=importlib.util.spec_from_file_location('lb1_final_frozen_db',snapshot)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
baseline=json.loads((root/'preexisting-dev-row-fingerprints.private.json').read_text())
conn=sqlite3.connect('file:'+str(prod/'cloud-trace-db.sqlite')+'?mode=ro',uri=True)
conn.row_factory=sqlite3.Row
assert conn.execute('PRAGMA integrity_check').fetchall()[0][0]=='ok'
assert conn.execute('PRAGMA foreign_key_check').fetchall()==[]
# Check only the exact original explicitly-DEV primary keys.
original_checks={}
pks={'corpus_tasks':('task_id',),'agent_traces':('trace_id',),'task_issues':('issue_id',),'trace_steps':('trace_id','step_index')}
for table,info in baseline['tables'].items():
    checked=0
    for key,expected in info['row_hashes'].items():
        values=json.loads(key)
        where=' AND '.join(column+'=?' for column in pks[table])
        row=conn.execute('SELECT * FROM '+table+' WHERE '+where,values).fetchone()
        assert row is not None,('original row missing',table)
        canonical=json.dumps(dict(row),ensure_ascii=False,sort_keys=True,separators=(',',':'))
        assert hashlib.sha256(canonical.encode()).hexdigest()==expected,('original row changed',table)
        checked+=1
    original_checks[table]={'unchanged':checked,'baseline_sha256':info['aggregate_sha256']}
run_results=[]
cas=module.S3CompatibleCAS(local_directory=prod/'cas',bucket='')
with module.CloudTraceDatabase(prod/'cloud-trace-db.sqlite') as db:
    for run in source_receipt['goodfire']['decoded_runs']:
        run_id='goodfire-reward-hack-data@bd8fb473568b8ef1d12fc2325a5c8af939c15ad9/'+run['run']
        rows=conn.execute('SELECT trace_id, step_count FROM agent_traces WHERE run_id=? ORDER BY trace_id',(run_id,)).fetchall()
        assert len(rows)==run['native_rows']
        assert sum(r['step_count'] for r in rows)==run['steps']
        actual_steps=conn.execute('SELECT count(*) FROM trace_steps s JOIN agent_traces a USING(trace_id) WHERE a.run_id=?',(run_id,)).fetchone()[0]
        assert actual_steps==run['steps']
        first=db.get_trace(rows[0]['trace_id'])
        native=json.loads(cas.get_bytes(first['raw_cas_uri']))['traces'][0]
        assert first['task']['split']=='held' and first['issues']==[]
        assert first['verified_receipt_json']['status']=='provisional'
        assert len(first['steps'])==len(native['nodes'])
        for built,node in zip(first['steps'],native['nodes'],strict=True):
            flags=built['flags_json'];restored=copy.deepcopy(flags['native_node_residual'])
            message=restored.get('message')
            if isinstance(message,dict):
                if flags['content_key_present']:
                    message['content']=json.loads(built['content_text']) if flags['content_encoding']=='canonical_json' else built['content_text']
                if flags['reasoning_content_key_present']:
                    message['reasoning_content']=flags['reasoning_content']
            assert restored==node
        run_results.append({'run':run['run'],'traces':len(rows),'steps':actual_steps,'sample_native_nodes_reconstructed':len(first['steps'])})
    wrench_ids=[json.loads(line)['task_id'] for line in (root/'goodfire-wrench/wrench/handoff-v3/tasks.jsonl').open()]
    wrench_traces=[]
    for task in wrench_ids:
        assert db.get_task(task)['split']=='held'
        wrench_traces.extend(conn.execute('SELECT trace_id,step_count,run_id FROM agent_traces WHERE task_id=?',(task,)).fetchall())
    assert len(wrench_traces)==126 and sum(r['step_count'] for r in wrench_traces)==1079
    assert len({r['run_id'] for r in wrench_traces})==72
    wrench_sample=db.get_trace(wrench_traces[0]['trace_id'])
    raw=json.loads(cas.get_bytes(wrench_sample['raw_cas_uri']))
    assert wrench_sample['verified_receipt_json']['status']=='provisional'
    for built,node in zip(wrench_sample['steps'],raw['steps'],strict=True):
        flags=built['flags_json'];restored=copy.deepcopy(flags['native_step_residual'])
        if flags['message_key_present']:
            restored['message']=json.loads(built['content_text']) if flags['content_encoding']=='canonical_json' else built['content_text']
        assert restored==node
        if built['observation_cas_uri']:
            assert json.loads(cas.get_bytes(built['observation_cas_uri']))==node['observation']
    assert source_receipt['exact_native_step_reconstructions']==source_receipt['new_step_rows']
    assert sum(r['traces'] for r in run_results)+len(wrench_traces)==source_receipt['new_trace_rows']
    assert sum(r['steps'] for r in run_results)+1079==source_receipt['new_step_rows']
counts={table:conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('corpus_tasks','task_issues','agent_traces','trace_steps')}
assert counts['task_issues']==252
report={'schema':'lb1.independent_final_trace_smoke/v1','created_at':datetime.now(UTC).isoformat(),
        'source_receipt_sha256':hashlib.sha256((prod/'corpus-expansion-lb1/safe-traces-ingestion.json').read_bytes()).hexdigest(),
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'integrity_check':'ok','foreign_key_check':'ok','goodfire_runs':run_results,
        'preexisting_rows':original_checks,
        'terminal_wrench':{'views':126,'run_lineages':72,'steps':1079,'reopened_sample_native_nodes_reconstructed':len(wrench_sample['steps'])},
        'all_new_trace_counts_and_step_counts_match':True,'all_new_native_steps_reconstructed_during_ingest':source_receipt['exact_native_step_reconstructions'],
        'table_counts':counts,'runtime_admissions':0,'queue_dispatches':0,'docker_runs':0,'cloud_connections':0,'benchmark_executions':0,
        'protected_registry_reads':0,'heldout_cleanliness_certified':False,
        'limitation':'No protected registry/body audit was performed; child mixed-source pre-clearance downloads/scans cannot be certified heldout-free.'}
conn.close()
with (prod/'corpus-expansion-lb1/final-trace-verification.json').open('x') as f:json.dump(report,f,indent=2,sort_keys=True);f.write('\n')
print(json.dumps(report,indent=2,sort_keys=True))
