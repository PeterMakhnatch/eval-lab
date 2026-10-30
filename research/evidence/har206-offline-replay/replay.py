"""Read-only HAR-206 replay; write JSON to stdout, never to source trials.

Arguments: published-results root, repository root, copied GEPA trial directory.
The GEPA source worktree is provenance only and is never accessed by this script.
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import evallab.copy_check as copy_module
from evallab.analysis_worker import eligible_trials
from evallab.copy_check import copy_check
from evallab.counts import attach_counts
from evallab.explorer import _trial_view
from evallab.harbor_view import _is_native, trial_integrity
from evallab.interpretation.features import trial_features
from evallab.mimo_exploit import trial_reward
from evallab.probe03 import read_reward
from evallab.process_job import _process_trial

root = Path(sys.argv[1]).resolve()
repo = Path(sys.argv[2]).resolve()
gepa_copy = Path(sys.argv[3]).resolve()
gepa_source = ('/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs/'
               'gepa-terminus-2-format-code-task-000-9089903b46c8c3c7410657ba/'
               'gepa-terminus-2-format-code-task__cNYdqfo')
BASELINE = '5ae3bfb42825815ed610276cea5e5461b159d844'


def baseline_module(name: str, filename: str):
    source = subprocess.check_output(
        ['git', 'show', f'{BASELINE}:{filename}'], cwd=repo, text=True
    )
    module = types.ModuleType(name)
    module.__file__ = str(repo / filename)
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


old_copy = baseline_module('har206_old_copy', 'src/evallab/copy_check.py')
old_features = baseline_module('har206_old_features', 'src/evallab/interpretation/features.py')
old_viewer = baseline_module('har206_old_viewer', 'src/evallab/harbor_view.py')
examples = [
    ('copy', '2026-10-06/HAR-161-har161r-exploit-002552/har161r-exploit-002552__JSmqRFJ'),
    ('copy', '2026-10-06/HAR-161-har161-exploit-002552/har161-exploit-002552__8HmxG9v'),
    ('copy', '2026-10-06/HAR-161-har161-exploit-002864/har161-exploit-002864__7EbpTbv'),
    ('copy', '2026-10-01/HAR-126-ovn-g5-000169-tuned/ovn-g5-000169-tuned__CxBpceW'),
    ('clean', '2026-10-09/HAR-202-chrr1-l-honest-002552/chrr1-l-honest-002552__4YXBF9m'),
    ('clean', '2026-10-01/HAR-126-ovn-g5-000169-gepa/ovn-g5-000169-gepa__Hmxf7wb'),
    ('unprocessed', '2026-10-06/HAR-171-har171-mimo-new-integrity-dims/66feb63b1db7__cxLs2vL'),
    ('000226_comparator', '2026-09-30/HAR-104-har104-d-000226~har104-runs/har104-d-000226__JCDfZFi'),
    ('usability', '2026-10-01/HAR-120-har120-001269-a1/har120-001269-a1__y3Wy5Ku'),
    ('usability', '2026-10-01/HAR-120-har120-001269-a2/har120-001269-a2__sA8cq7s'),
    ('usability', '2026-10-01/HAR-120-har120-001269-a2-r2/har120-001269-a2-r2__93WpuaE'),
]
examples.append(('gepa_cited', str(gepa_copy)))
for job in sorted((root / '2026-10-09').glob('unknown-*')):
    if not (job.name.startswith('unknown-tamper-') or job.name in ('unknown-git-002552-git2', 'unknown-calib-2402-skip')):
        continue
    for attempt in sorted(job.glob('*/agent/cheat/attempts.json')):
        examples.append(('red_team', str(attempt.parent.parent.parent.relative_to(root))))

def sha(path: Path):
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()

rows = []
for cohort, rel in examples:
    trial = root / rel
    job = trial.parent
    result = json.loads((trial / 'result.json').read_text())
    old_path = job / 'processed' / ('trial-' + trial.name + '.json')
    inputs = [trial / 'result.json', trial / 'agent/trajectory.json', trial / 'agent/terminus_2.pane',
              trial / 'verifier/agent.diff', trial / 'agent/cheat/attempts.json', old_path]
    if cohort == 'gepa_cited':
        inputs = sorted(p for p in trial.rglob('*') if p.is_file()) + [old_path]
    hashes_before = {str(p): sha(p) for p in inputs}
    old = json.loads(old_path.read_text()) if old_path.is_file() else {}
    before = (old.get('counts') or {}).get('verdict', 'unprocessed')
    old_copy_stored = any(x.get('kind') == 'copied_code' for x in old.get('taint', []) if isinstance(x, dict))
    current_detector = copy_module.copy_check
    copy_module.copy_check = old_copy.copy_check
    try:
        replayed_copy_before = old_copy.copy_check(trial)
        feature_before = old_features.trial_features(job, trial)
        rewards = ((result.get('verifier_result') or {}).get('rewards') or {})
        viewer_before = (rewards['integrity'] if old_viewer._is_native(rewards)
                         else old_viewer.trial_integrity(trial, job)['integrity'])
    finally:
        copy_module.copy_check = current_detector
    flag = copy_check(trial)
    record = _process_trial(trial, job)
    after = attach_counts(record, result, label_root=repo)
    feature = trial_features(job, trial)
    viewer = trial_integrity(trial, job)
    viewer_after = (rewards['integrity'] if _is_native(rewards) and not viewer['fired']
                    else viewer['integrity'])
    if hashes_before != {str(p): sha(p) for p in inputs}:
        raise RuntimeError(f'replay changed an input: {trial}')
    rows.append({
        'cohort': cohort, 'trial': gepa_source if cohort == 'gepa_cited' else rel,
        'evidence_location': 'read-only scratch copy' if cohort == 'gepa_cited' else 'published results',
        'raw_reward': record.get('reward'),
        'count_before': before, 'count_after': after['verdict'], 'reasons_after': after['reasons'],
        'copy_before_stored': old_copy_stored, 'copy_after': flag is not None,
        'copy_before_replayed': replayed_copy_before is not None,
        'copy_method': flag.get('match_method') if flag else None,
        'copy_source_artifact': flag.get('source_artifact') if flag else None,
        'feature_verdict_before': feature_before.get('copy_verdict'),
        'feature_verdict_after': feature.get('copy_verdict'),
        'viewer_integrity_before': viewer_before,
        'viewer_integrity_after': viewer_after, 'viewer_rules_after': viewer['fired'],
        'input_sha256': {str(p.relative_to(trial)): hashes_before[str(p)] for p in inputs[:-1] if hashes_before[str(p)] is not None},
        'processed_sha256': hashes_before[str(old_path)],
        'input_hashes_unchanged': True,
    })
    if cohort == 'gepa_cited':
        if not result.get('exception_info') or rewards.get('reward') != 1.0:
            raise ValueError('GEPA copy does not have the cited scored-exception outcome')
        view = _trial_view(job.name, trial, job.parent)
        worker_reason = eligible_trials([SimpleNamespace(trials=[SimpleNamespace(path=trial)])])[0][2]
        rows[-1]['scored_exception_consumers'] = {
            'explorer_before': 'infra-exception',
            'explorer_after': view.outcome_class.value,
            'worker_before': 'defer:harness_exception_not_agent_failure',
            'worker_after': worker_reason or 'eligible',
            'mimo_before_reward': float(rewards['reward']),
            'mimo_after_reward': trial_reward(trial),
        }

# A bounded, explicitly defined scan of published trials through Oct 9.
masked = []
for day in sorted(root.glob('2026-*')):
    if day.name > '2026-10-09':
        continue
    for f in day.glob('*/*/result.json'):
        try:
            result = json.loads(f.read_text())
            if not result.get('exception_info'):
                continue
            rewards = ((result.get('verifier_result') or {}).get('rewards') or {})
            value = rewards.get('reward')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value == -1:
                continue
            masked.append((f.parent, value))
        except (OSError, ValueError, AttributeError, TypeError):
            continue
consumer_counts = collections.defaultdict(collections.Counter)
masked_rows = []
for trial, value in masked:
    result_path = trial / 'result.json'
    result_hash = sha(result_path)
    expected = 'pass' if value >= 1 else 'fail'
    view = _trial_view(trial.parent.name, trial, trial.parent.parent)
    observed = {'pass': 'pass', 'reward-failure': 'fail'}.get(view.outcome_class.value, str(view.outcome_class.value))
    worker_reason = eligible_trials([SimpleNamespace(trials=[SimpleNamespace(path=trial)])])[0][2]
    reward = trial_reward(trial)
    consumer_counts['explorer_before']['infra-exception'] += 1
    consumer_counts['explorer_after'][observed] += 1
    consumer_counts['worker_before']['defer_exception'] += 1
    consumer_counts['worker_after']['eligible' if worker_reason is None else worker_reason] += 1
    consumer_counts['mimo_before'][expected] += 1
    consumer_counts['mimo_after']['pass' if reward is not None and reward >= 1 else 'fail' if reward is not None else 'missing'] += 1
    consumer_counts['counts_raw_before'][expected] += 1
    raw, scored, _ = read_reward(trial)
    consumer_counts['counts_raw_after']['pass' if scored and raw is not None and raw >= 1 else 'fail' if scored else 'unscored'] += 1
    if result_hash != sha(result_path):
        raise RuntimeError(f'replay changed a scored-exit result: {result_path}')
    masked_rows.append({'trial': str(trial.relative_to(root)), 'raw_reward': value,
                        'result_sha256': result_hash, 'input_hash_unchanged': True})

cohort_counts = {}
for cohort in sorted({row['cohort'] for row in rows}):
    selected = [row for row in rows if row['cohort'] == cohort]
    cohort_counts[cohort] = {
        key: dict(collections.Counter(str(row[key]) for row in selected))
        for key in ('count_before', 'count_after', 'copy_before_stored', 'copy_before_replayed', 'copy_after',
                    'feature_verdict_before', 'feature_verdict_after',
                    'viewer_integrity_before', 'viewer_integrity_after')
    }

out = {
    'schema': 'har206.saved-replay/v1',
    'scope': 'Published results dated through 2026-10-09 with exception and finite numeric primary reward; not the original 3,879-record cohort.',
    'baseline_revision': BASELINE,
    'baseline': 'Stored processed records for counts/copy; baseline source modules at baseline_revision for features and viewer (with baseline copy detector); pre-change explorer and worker branches for numeric scored exceptions; mimo_exploit baseline already retained numeric exception scores.',
    'limitations': ['The cited GEPA trial was replayed from a scratch copy of the source trial, not in its source worktree. The HAR104 000226 trial is a separate unprocessed control.', 'The original 3,879-record cohort and its exact 41/190 scored-exception subset were not reconstructed; the bounded published-results scan is a different cohort.', 'Counts after use current committed usability and audit labels, so changes in those labels are distinct from detector changes.', 'Features copy_verdict does not include fetch taint or usability.'],
    'examples': rows,
    'cohort_counts': cohort_counts,
    'scored_exception_trials': masked_rows,
    'consumer_counts': {name: dict(counter) for name, counter in consumer_counts.items()},
}
print(json.dumps(out, indent=2, sort_keys=True))
