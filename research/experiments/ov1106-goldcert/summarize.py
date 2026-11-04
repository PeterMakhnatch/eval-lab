"""Summarize private Gold receipts without model, shell, or execution calls.

Required: --generation DIR --prepared PATH --cells DIR --out DIR.
Optional --inputs and --source-verification default to the lane's private receipts.
--freeze defaults to contract-freeze.json beside --prepared. --cell-revision
selects exactly one cell namespace (default: prepared['revision']); no retry picking.
Cells: {task}-{original|gold}-{revision}-{control|alternative-INDEX}/result.json.
--original-cell-revision / --gold-cell-revision override control namespaces only.
Alternative cells always use --cell-revision (or the prepared revision).
Cell scripts must be agent-script.sh, bound by the Modal 300-second cache key.

Current driver schema: frozen/{binding,manifest,tools}.json, frozen driver/transport,
accepted-canary.json or canary/receipt.json, gateway-envelopes.jsonl, and
writers/TASK/INDEX/{receipt,messages,tool-results}.json plus solution.patch.
Only hashes and aggregate status leave this script. U means unmeasured.
G3/G4 stay U: this safe matrix has no current-contract integrity evidence schema.
A standalone public scorer is NOT a hardened Gold certificate.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import math
import sys
from datetime import datetime
from functools import cache
from pathlib import Path

TASKS = ('000682', '002139', '000081', '001267', '003007')
MODEL = 'openai-codex/gpt-6.1-terra'
DRIVER_SHA = '170ed8f02f9edb9f9bf93a433c54fb873d34c92d54256687bde02d0cae441e89'
TRANSPORT_SHA = 'f2e8c41fd72e01fc0e0b75b0ef190cde3aa2e92c0321152f995fc5cf52a7d22a'
PREFIXES = {'000682': ('c7n/',), '002139': ('lib/bb/',), '000081': ('xdoctest/',),
            '001267': ('redash/', 'rd_ui/app/'), '003007': ('sucks/',)}
HERE = Path(__file__).resolve().parent
PRIVATE = Path('/private/tmp/ov1106/goldcert')

sys.path.insert(0, str(HERE.parents[2] / 'src'))
from evallab.registry import task_directory_digest  # noqa: E402


@cache
def package_digest(path):
    return task_directory_digest(Path(path))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def load(path):
    return json.loads(Path(path).read_bytes())


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def checked(path, expected=None):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'missing/nonregular evidence file')
    data = path.read_bytes()
    require(expected is None or digest(data) == expected, 'evidence hash mismatch')
    return data


def literal(path, name):
    for node in ast.parse(checked(path)).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('missing frozen constant: ' + name)


def production(task, name):
    path = Path(name)
    return (not path.is_absolute() and path.as_posix() == name and '..' not in path.parts
            and name.startswith(PREFIXES[task]) and path.suffix.lower() in {'.py', '.js', '.jsx', '.html', '.css'}
            and all(not (p.startswith('.') or p in {'test', 'tests', 'testing', 'conftest.py',
                'sitecustomize.py', 'usercustomize.py', 'setup.py'} or p.startswith(('test_', 'testing_'))
                or p.endswith(('_test.py', '_tests.py', '_test.js', '_tests.js', '_test.jsx', '_tests.jsx'))
                or '.test.' in p or '.spec.' in p) for p in (s.lower() for s in path.parts)))


def prompt(task, instruction):
    return [{'role': 'user', 'content': 'Implement this task in the public source repository at /workspace. Read instruction.md and source files with bash. You see only pristine public source and the instruction, never tests, references, prior solutions, or grader feedback. Edit production source only, including new modules under these approved prefixes: ' + ', '.join(PREFIXES[task]) + '. Do not remove existing files or add tests, startup hooks, or bypasses. This minimal Debian container has shell tools but no Python or git. No network is available. Complete the implementation, then describe changed production paths.\n\n' + instruction.decode()}]


def binding(root, trusted_manifest):
    frozen = root / 'frozen'
    bound = load(frozen / 'binding.json')
    require(bound['model'] == MODEL and bound['driver_sha256'] == DRIVER_SHA
            and bound['transport_sha256'] == TRANSPORT_SHA, 'unreviewed driver/transport/model')
    checked(frozen / 'ov1106-goldcert/generate_blind.py', bound['driver_sha256'])
    checked(frozen / 'ov1104-impossible/container_tool.py', bound['transport_sha256'])
    checked(frozen / 'manifest.json', digest(checked(trusted_manifest)))
    require(bound['manifest_sha256'] == digest(checked(trusted_manifest)), 'manifest binding mismatch')
    tools = load(frozen / 'tools.json')
    require(digest(encoded(tools)) == bound['tool_schema_sha256'], 'tool schema binding mismatch')
    driver = frozen / 'ov1106-goldcert/generate_blind.py'
    require(tools == literal(driver, 'TOOLS') and bound['image'] == literal(driver, 'IMAGE')
            and bound['session_seconds'] == literal(driver, 'SESSION_SECONDS'), 'frozen constants mismatch')
    return bound, tools


def gateway(root):
    requests, responses, subjects = {}, {}, {}
    for line in checked(root / 'gateway-envelopes.jsonl').splitlines():
        row = json.loads(line)
        value = row['envelope']
        require(digest(encoded(value)) == row['sha256'], 'gateway envelope hash mismatch')
        if row['kind'] == 'request':
            request = value['request']
            ident = str(request['id'])
            require(ident not in requests, 'duplicate gateway request')
            requests[ident] = value
            subjects.setdefault(value['subject'], []).append(ident)
        elif row['kind'] == 'response':
            ident = str(value['id'])
            require(ident in requests and ident not in responses, 'unmatched/duplicate gateway response')
            require(value.get('status') == 200 and value.get('body', {}).get('model') == MODEL,
                    'gateway error or model mismatch; entire batch quarantined')
            responses[ident] = value
    require(set(requests) == set(responses), 'unanswered gateway requests; incomplete batch')
    return requests, responses, subjects


def chain(root, subject, bound, tools, ids, messages, records, initial, audit):
    requests, responses, subjects = audit
    require(ids and ids == subjects.get(subject) and len(ids) == len(set(ids)), 'subject request IDs mismatch')
    rebuilt, observed, seen = list(initial), [], set()
    offset = 0
    for step, ident in enumerate(ids):
        row = requests[ident]
        request = row['request']
        body = request['body']
        require(row['binding'] == bound and request['path'] == '/v1/chat/completions'
                and body['model'] == MODEL and body['tools'] == tools
                and body['messages'] == rebuilt and body.get('tool_choice') == 'auto'
                and body.get('max_tokens') == 18000, 'raw request/prompt/schema mismatch')
        choice, = responses[ident]['body']['choices']
        message = choice['message']
        require(message['role'] == 'assistant', 'nonassistant response')
        rebuilt.append(message)
        calls = message.get('tool_calls') or []
        if not calls:
            require(choice['finish_reason'] == 'stop' and step == len(ids)-1 and observed,
                    'incomplete final response')
        else:
            require(choice['finish_reason'] == 'tool_calls' and step < len(ids)-1, 'unfinished tool chain')
        for call in calls:
            cid = call['id']
            require(cid and cid not in seen and call['function']['name'] == 'bash', 'invalid tool ID/name')
            seen.add(cid)
            params = json.loads(call['function']['arguments'])
            require(set(params) == {'command'} and isinstance(params['command'], str), 'invalid tool arguments')
            record = records[offset]
            offset += 1
            require(record['tool_call_id'] == cid and record['request_id'] == ident
                    and record['result']['command'] == params['command'], 'tool result mismatch')
            observed.append(record)
            rebuilt.append({'role': 'tool', 'tool_call_id': cid, 'content': json.dumps(record['result'])})
    require(offset == len(records) and rebuilt == messages, 'transcript/tool record mismatch')
    return {'messages_sha256': digest(checked(root / ('canary/messages.json' if subject == 'canary' else 'writers/' + subject + '/messages.json'))),
            'requests': len(ids), 'tools': len(records)}


def canary(root, bound, tools, inputs):
    accepted = root / 'accepted-canary.json'
    if accepted.exists():
        admission = load(accepted)
        path = Path(admission['receipt_path'])
        checked(path, admission['receipt_sha256'])
        proof = load(path)
        require(proof == admission['receipt'], 'accepted canary object mismatch')
        canary_root = path.parent.parent
    else:
        canary_root = root
        proof = load(root / 'canary/receipt.json')
    other, other_tools = binding(canary_root, inputs)
    require(other == bound and other_tools == tools and proof['binding'] == bound
            and proof.get('passed') is True and proof.get('cleanup_ok') is True
            and proof.get('error') is None, 'failed/unbound canary')
    positive = literal(canary_root / 'frozen/ov1104-impossible/container_tool.py', 'POSITIVE')
    commands = ['ls /Users', 'cat ~/.zshrc', 'ls /private/tmp/ov1104', 'ls /private/tmp/ov1106', positive]
    probes = proof['probes']
    require(len(probes) == 5 and {r['result']['command'] for r in probes} == set(commands), 'missing canary probes')
    for record in probes:
        result = record['result']
        require((result['returncode'] == 0 and result['output'].strip() == '/workspace') if result['command'] == positive
                else result['returncode'] not in (0, 125), 'canary denial/positive failure')
    initial = [{'role': 'user', 'content': 'Use the bash tool for exactly these commands separately. Use no other commands. Then stop.\n' + '\n'.join(commands)}]
    chain(canary_root, 'canary', bound, tools, proof['request_ids'], load(canary_root / 'canary/messages.json'),
          probes, initial, gateway(canary_root))
    return proof


def public_inputs(inputs, verification):
    manifest = load(inputs)
    require(len(manifest['tasks']) == 5 and {r['id'] for r in manifest['tasks']} == set(TASKS), 'changed input cohort')
    source = load(verification)
    verified = {r['task_id']: r for r in source['tasks']}
    require(len(source['tasks']) == 5 and set(verified) == set(TASKS), 'changed source-verification cohort')
    tasks = {}
    for task in manifest['tasks']:
        tid = task['id']
        receipt = verified[tid]
        require(receipt.get('container_never_started') is True and receipt.get('cleanup_ok') is True
                and receipt['image'] == task['source_image'], 'source image verification failed')
        require(len(receipt['checked_files']) == len(task['sources']) and
                {f['path']: f['sha256'] for f in receipt['checked_files']} == task['source_hashes']
                and set(task['sources']) == set(task['source_hashes'])
                and tuple(task['allowed_prefixes']) == PREFIXES[tid] and task['upstream_revision'], 'pristine source manifest mismatch')
        for name, path in task['sources'].items():
            require(production(tid, name), 'nonproduction pristine input')
            checked(path, task['source_hashes'][name])
        checked(task['instruction'], task['instruction_sha256'])
        tasks[tid] = task
    return tasks


def candidate(root, tid, index, task, bound, tools, proof, audit):
    directory = root / 'writers' / tid / str(index)
    receipt_path = directory / 'receipt.json'
    receipt = load(receipt_path)
    split = 'validation' if index < 2 else 'held_out'
    require(receipt['task'] == tid and receipt['index'] == index and receipt['frozen_split'] == split,
            'changed candidate identity/split')
    require(receipt.get('eligible_for_execution') is True and receipt.get('error') is None
            and receipt.get('cleanup_ok') is True and receipt['binding'] == bound, 'failed candidate receipt')
    require(all(receipt.get(k) == v for k, v in bound.items()), 'candidate top-level binding mismatch')
    require(receipt['model_requested'] == MODEL and receipt['models_served'] == [MODEL]
            and receipt['canary_sha256'] == digest(encoded(proof))
            and receipt['instruction_sha256'] == task['instruction_sha256']
            and receipt['source_files'] == task['source_hashes'], 'candidate provenance mismatch')
    patch = checked(directory / 'solution.patch', receipt['patch_sha256'])
    require(Path(receipt['patch']).resolve() == (directory / 'solution.patch').resolve()
            and receipt.get('quarantined_patch') is None and patch, 'patch admission mismatch')
    paths = [line[6:] for line in patch.decode().splitlines() if line.startswith('+++ b/')]
    require(paths and len(paths) == len(set(paths)) and sorted(paths) == sorted(receipt['modified_paths'])
            and all(production(tid, name) for name in paths), 'nonproduction/invalid patch paths')
    records = load(directory / 'tool-results.json')
    require(receipt['tool_names'] == ['bash'] and receipt['tool_calls'] == len(records), 'tool count mismatch')
    details = chain(root, tid + '/' + str(index), bound, tools, receipt['request_ids'],
                    load(directory / 'messages.json'), records, prompt(tid, checked(task['instruction'])), audit)
    return dict(details, receipt_sha256=digest(checked(receipt_path)), patch_sha256=digest(patch))


def cell_key(package_digest, variant, script):
    return digest('\0'.join((package_digest, variant, digest(script), 'modal-default', '', '300')).encode())


def cell(cells, name, task, version, script, markers, freeze_time):
    directory = cells / name
    path = directory / 'result.json'
    try:
        result = load(path)
        require(package_digest(task[version + '_package']) == task[version + '_digest'], 'prepared package drift')
        require(checked(directory / 'agent-script.sh') == script, 'cell script mismatch')
        require(result['cell_key'] == cell_key(task[version + '_digest'], name, script), 'cell package/script key mismatch')
        log = result.get('agent_log', '')
        require(result.get('status') == 'executed' and type(result.get('reward')) in (int, float)
                and result['reward'] in (0, 1), 'cell not executed or reward missing')
        require(all(marker in log for marker in markers), 'patch/nop applied marker missing')
        require(path.stat().st_mtime > freeze_time, 'score predates contract freeze')
        return {'reward': result['reward'], 'status': 'executed', 'receipt_sha256': digest(checked(path)),
                'script_sha256': digest(script)}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'reward': None, 'status': 'unmeasured', 'reason': type(exc).__name__ + ': ' + str(exc)}


def markers(script):
    text = script.decode()
    # Require every source application in legacy reference+mutant scripts, not just the last marker.
    return [m for m in ('SOURCE_PATCH_APPLIED', 'REFERENCE_APPLIED', 'MUTANT_APPLIED', 'SAFE_NOP', 'NOP_APPLIED') if m in text]


def alternative_script(patch, workdir):
    return (f'set -eu\ncd {workdir}\nprintf %s {base64.b64encode(patch).decode()} | base64 -d > /tmp/public-source.patch\n'
            'git apply --whitespace=nowarn /tmp/public-source.patch\necho SOURCE_PATCH_APPLIED\n').encode()


def gate(rewards, desired):
    if any(r is not None and r != desired for r in rewards):
        return 'FAIL'
    return 'PASS' if rewards and all(r == desired for r in rewards) else 'U'


def proportion(k, n):
    # Descriptive only: candidates within a task are not independent task samples.
    z = 1.959964
    p = k/n
    den = 1 + z*z/n
    mid = (p + z*z/(2*n))/den
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))/den
    return {'accepted': k, 'planned': n, 'wilson95_descriptive': [max(0, mid-half), min(1, mid+half)]}


def summarize(args):
    prepared = load(args.prepared)
    require(set(prepared['tasks']) == set(TASKS), 'changed prepared cohort')
    frozen = load(args.freeze)
    freeze_time = max(datetime.fromisoformat(frozen['frozen_at']).timestamp(), args.freeze.stat().st_mtime)
    drift = []
    for name, expected in frozen.items():
        if name in {'frozen_at', 'alternative_informed'}:
            continue
        path = args.prepared if name == 'prepared_manifest_sha256' else HERE / name
        if not path.is_file() or digest(checked(path)) != expected:
            drift.append(name)
    require('prepared_manifest_sha256' in frozen, 'freeze missing prepared binding')
    revision = args.cell_revision or prepared['revision']
    control_revisions = {'original': args.original_cell_revision or revision,
                         'gold': args.gold_cell_revision or revision}
    common_error = None
    try:
        tasks = public_inputs(args.inputs, args.source_verification)
        bound, tools = binding(args.generation, args.inputs)
        proof = canary(args.generation, bound, tools, args.inputs)
        audit = gateway(args.generation)
    except (OSError, ValueError, KeyError, TypeError, IndexError, SyntaxError) as exc:
        common_error = type(exc).__name__ + ': ' + str(exc)
    slots, controls, table = [], [], []
    for tid in TASKS:
        task = prepared['tasks'][tid]
        by_version = {}
        for version in ('original', 'gold'):
            outcomes = {}
            for control in task['controls']:
                name = f'{tid}-{version}-{control_revisions[version]}-{control["name"]}'
                script = control['script'].encode()
                required = markers(script)
                outcome = cell(args.cells, name, task, version, script, required, freeze_time)
                if not required:
                    outcome = {'reward': None, 'status': 'unmeasured', 'reason': 'no applied-marker schema'}
                outcomes[control['name']] = outcome
                controls.append(dict(task=tid, version=version, control=control['name'], arm=control['arm'], **outcome))
            by_version[version] = outcomes
        for index in range(4):
            row = {'task': tid, 'index': index, 'split': 'validation' if index < 2 else 'held_out', 'provenance': False}
            try:
                require(common_error is None, common_error)
                row.update(candidate(args.generation, tid, index, tasks[tid], bound, tools, proof, audit))
                row['provenance'] = True
            except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
                row['reason'] = type(exc).__name__ + ': ' + str(exc)
            for version in ('original', 'gold'):
                if row['provenance']:
                    patch = checked(args.generation / 'writers' / tid / str(index) / 'solution.patch')
                    name = f'{tid}-{version}-{revision}-alternative-{index}'
                    row[version] = cell(args.cells, name, task, version, alternative_script(patch, task['workdir']),
                                        ['SOURCE_PATCH_APPLIED'], freeze_time)
                else:
                    row[version] = {'reward': None, 'status': 'unmeasured', 'reason': 'candidate provenance not admitted'}
            slots.append(row)
        gold = by_version['gold']
        ref = [gold[c['name']]['reward'] for c in task['controls'] if c['arm'] == 'reference']
        nop = [gold[c['name']]['reward'] for c in task['controls'] if c['arm'] == 'nop']
        train = [gold[c['name']]['reward'] for c in task['controls'] if c['arm'] == 'train']
        evaluation = [gold[c['name']]['reward'] for c in task['controls'] if c['arm'] == 'eval']
        original_eval = [by_version['original'][c['name']]['reward'] for c in task['controls'] if c['arm'] == 'eval']
        g6 = gate(evaluation, 0)
        if g6 == 'PASS' and (not original_eval or None in original_eval):
            g6 = 'U'
        alternative_rewards = [r['gold']['reward'] for r in slots if r['task'] == tid]
        table.append({'task': tid, 'G1': gate(ref, 1), 'G2': gate(nop, 0), 'G3': 'U', 'G4': 'U',
                      'G5': gate(train, 0), 'G6': g6, 'G7': gate(alternative_rewards, 1), 'certified': False,
                      'train_killed': sum(r == 0 for r in train), 'train_planned': len(train),
                      'eval_killed': sum(r == 0 for r in evaluation), 'eval_planned': len(evaluation)})
    # Drift is not treated as clean held-out evidence, even if candidate scores exist.
    informed = bool(drift) or prepared.get('alternative_informed') is True or frozen.get('alternative_informed') is True
    if drift:
        for row in table:
            for name in ('G1', 'G2', 'G5', 'G6', 'G7'):
                if row[name] == 'PASS':
                    row[name] = 'U'
    counts = {}
    for version in ('original', 'gold'):
        for split in ('all', 'validation', 'held_out'):
            group = [r for r in slots if split == 'all' or r['split'] == split]
            counts[version + '_' + split] = dict(proportion(sum(r[version]['reward'] == 1 for r in group), len(group)),
                                                 measured=sum(r[version]['reward'] is not None for r in group))
    return {'schema': 'ov1106-provenance-summary-v1', 'tasks': list(TASKS), 'planned_slots': 20,
            'input_hashes': {'prepared': digest(checked(args.prepared)), 'freeze': digest(checked(args.freeze)),
                             'inputs': digest(checked(args.inputs)), 'source_verification': digest(checked(args.source_verification))},
            'cell_revision': revision, 'control_revisions': control_revisions,
            'contract_drift': drift, 'alternative_informed': informed,
            'held_out_claim_admissible': not informed and common_error is None
                and all(r['provenance'] for r in slots if r['split'] == 'held_out'),
            'generation_error': common_error,
            'counts': counts, 'gates': table, 'slots': slots, 'controls': controls,
            'limits': ['Standalone public scorer is not a hardened Gold certificate.',
                       'Original acceptance alone does not establish correctness.',
                       'Wilson intervals are descriptive, not independent task-level inference.',
                       'Exact prompt and chain checks establish structural provenance, not semantic proof of no prior model knowledge.',
                       'Source-image receipt attests a reviewed export, not cryptographic upstream identity.',
                       'G3/G4 remain U: safe matrix provides no measured current-contract integrity controls.',
                       'Freeze timestamps and score mtimes establish local order only, not signed chronology.',
                       'Missing, errored, or quarantined candidates remain in the fixed denominator.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('generation', 'prepared', 'cells', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=PRIVATE / 'frozen-inputs.json')
    parser.add_argument('--source-verification', type=Path, default=PRIVATE / 'source-image-verification.json')
    parser.add_argument('--freeze', type=Path)
    parser.add_argument('--cell-revision')
    parser.add_argument('--original-cell-revision')
    parser.add_argument('--gold-cell-revision')
    args = parser.parse_args()
    args.freeze = args.freeze or args.prepared.parent / 'contract-freeze.json'
    result = summarize(args)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'summary.json').write_text(json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + '\n')
    lines = ['# Gold public-contract summary', '', 'No hardened Gold certificate is established.', '',
             '| Task | G1 | G2 | G3 | G4 | G5 | G6 | G7 | Certified |', '|---|---|---|---|---|---|---|---|---|']
    for row in result['gates']:
        lines.append('| ' + ' | '.join(str(row[k]) for k in ('task', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7', 'certified')) + ' |')
    lines += ['', f'Planned slots: 20. Alternative-informed: {result["alternative_informed"]}.', '',
              '## Acceptance (not correctness)', '', '| Version/split | Accepted/planned | Measured | Wilson 95% (descriptive) |', '|---|---|---|---|']
    for name, count in result['counts'].items():
        lo, hi = count['wilson95_descriptive']
        lines.append(f'| {name} | {count["accepted"]}/{count["planned"]} | {count["measured"]} | {lo:.4f}–{hi:.4f} |')
    lines += ['', '## Limits', ''] + ['- ' + text for text in result['limits']]
    (args.out / 'summary.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'planned_slots': 20, 'counts': result['counts'], 'alternative_informed': result['alternative_informed']}, sort_keys=True))


if __name__ == '__main__':
    main()
