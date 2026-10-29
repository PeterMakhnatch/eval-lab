import hashlib, json, random
from pathlib import Path
from evallab.production.run_atlas import connect_readonly, admitted_task_ids, cloud_trace_ids, DEFAULT_DATABASE
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.trace_harness import GOODFIRE_DATASET
p=Path.home()/'.local/state/evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl'
data=p.read_bytes()
assert hashlib.sha256(data).hexdigest()=='3e18a9e4c01058f2f4b7678db10f178b9c785edda72a7c37e0aeb7d374caec69'
pairs=[json.loads(line) for line in data.splitlines()]
ids=[p[k] for p in pairs for k in ('clean','enacted')]
assert len(ids)==246 and len(set(ids))==246
with connect_readonly(DEFAULT_DATABASE) as conn:
    admitted=set(cloud_trace_ids(conn, admitted=admitted_task_ids(conn, CorpusRegistry()), dataset_ids=[GOODFIRE_DATASET], trace_ids=ids))
assert set(ids)<=admitted, f'missing {len(set(ids)-admitted)}'
terra=ids.copy(); random.Random(40101).shuffle(terra)
subset=random.Random(40102).sample(pairs,20)
sol=[p[k] for p in subset for k in ('clean','enacted')]; random.Random(40103).shuffle(sol)
root=Path.home()/'.local/state/evallab-prod-live/trace-harness/np40'
(root/'terra-ids.txt').write_text('\n'.join(terra)+'\n')
(root/'sol-ids.txt').write_text('\n'.join(sol)+'\n')
print('strict goodfire-dev admission',len(set(ids)&admitted),'/',len(ids),'terra',len(terra),'sol',len(sol))
