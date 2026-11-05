"""Stage label-free DEV inputs, then execute the native categorical line after freeze."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path

from evaluate import LANE, ROOT, check_admission, digest, gates, keyed, rows

POLICY_SOURCE = Path('/Users/petermakhnatch/Developer/eval-lab-results/line/replay-v1/spec.json')
sys.path.insert(0, str(ROOT / 'research/experiments/ov1104-detbench'))
from detbench_lib import view_to_atif  # noqa: E402


def dump(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def _apply_section(before: str, section: str) -> str:
    """Apply complete unified hunks exactly, with no fuzzy or offset matching."""
    lines = before.splitlines(keepends=True)
    patch = section.splitlines(keepends=True)
    start = next(i for i, line in enumerate(patch) if line.startswith('@@ '))
    body = []
    for line in patch[start:]:
        if line.startswith('\\ No newline at end of file'):
            if not body:
                raise ValueError('orphan newline marker')
            body[-1] = body[-1].removesuffix('\n')
        else:
            body.append(line)
    result, cursor, index = [], 0, 0
    while index < len(body):
        match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?', body[index])
        if match is None:
            raise ValueError('incomplete or invalid hunk')
        old_start, old_count, new_start, new_count = (
            int(value) if value is not None else 1 for value in match.groups()
        )
        old_at = old_start - 1 if old_count else old_start
        new_at = new_start - 1 if new_count else new_start
        if not cursor <= old_at <= len(lines):
            raise ValueError('out-of-order hunk')
        result.extend(lines[cursor:old_at])
        cursor = old_at
        if len(result) != new_at:
            raise ValueError('hunk coordinates differ')
        removed = added = 0
        index += 1
        while index < len(body) and not body[index].startswith('@@ '):
            line = body[index]
            if line[:1] not in (' ', '-', '+'):
                raise ValueError('invalid hunk body')
            if line[0] in ' -':
                if cursor >= len(lines) or lines[cursor] != line[1:]:
                    raise ValueError('hunk context differs')
                cursor += 1
                removed += 1
            if line[0] in ' +':
                result.append(line[1:])
                added += 1
            index += 1
        if (removed, added) != (old_count, new_count):
            raise ValueError('truncated hunk')
    result.extend(lines[cursor:])
    return ''.join(result)


def without_preparation(diff_text: str, task: Path, variant: str) -> tuple[str, dict]:
    """Remove only exact declared preparation; preserve every action and changed section.

    Legacy snapshots follow restoration, not submission. This cannot recover erased
    agent changes. The action trace remains authoritative for those changes.
    """
    from evallab.hack_drop import parse_diff_sections

    if Path(variant).name != variant or variant in ('.', '..'):
        raise ValueError('invalid task variant')
    generated_path = task / 'generated.json'
    generated = json.loads(generated_path.read_text())
    inputs = {str(generated_path.resolve()): digest(generated_path)}
    prepared = {}
    for name in generated['test_files']:
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('invalid prepared path')
        source = task / 'variants' / variant / rel
        if source.is_file():
            prepared[name] = source.read_bytes().decode('utf-8')
            inputs[str(source.resolve())] = digest(source)
    # The declared archive expands before the agent runs. Read regular members
    # in memory; never extract paths or follow archive links on the host.
    archive = prepared.get('mimo_build_env.tar.gz.b64')
    if archive is not None:
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(archive)), mode='r:gz') as bundle:
            for member in bundle:
                rel = Path(member.name)
                if rel.is_absolute() or '..' in rel.parts or not member.isfile():
                    continue
                stream = bundle.extractfile(member)
                if stream is not None:
                    prepared[str(rel)] = stream.read().decode('utf-8')
    kept, removed = [], []
    for chunk in re.split(r'(?=^(?:diff (?:--git|-ruN) |Binary files ))', diff_text, flags=re.M):
        sections = parse_diff_sections(chunk)
        if len(sections) != 1:
            kept.append(chunk)
            continue
        section = sections[0]
        name = section['new_path'] or section['old_path']
        if name not in prepared or section['is_deleted']:
            kept.append(chunk)
            continue
        base = task / 'base' / name
        before = base.read_bytes().decode('utf-8') if base.is_file() else ''
        inputs[str(base.resolve())] = digest(base) if base.is_file() else None
        try:
            after = _apply_section(before, chunk)
        except (ValueError, StopIteration):
            kept.append(chunk)
            continue
        if after != prepared[name]:
            kept.append(chunk)
            continue
        removed.append({'path': name, 'section_sha256': hashlib.sha256(chunk.encode()).hexdigest()})
    result = ''.join(kept)
    return result, {'schema': 'ov1106.native_preparation/v1', 'task': str(task.resolve()),
                    'variant': variant, 'inputs': inputs, 'removed': removed,
                    'source_diff_sha256': hashlib.sha256(diff_text.encode()).hexdigest(),
                    'result_diff_sha256': hashlib.sha256(result.encode()).hexdigest(),
                    'limitation': 'post-restoration snapshot; submitted edits remain in actions only'}


def prepare(args):
    admission = check_admission(args.admitted)
    manifest = keyed(rows(Path(admission['cohort_manifest'])))
    views = keyed(rows(args.runs))
    if set(views) != set(manifest):
        raise ValueError('native line requires exactly the admitted view sample')
    args.out.mkdir(parents=True, exist_ok=False)
    rollout_dir = args.out / 'rollouts'
    rollout_dir.mkdir()
    inputs, native_rows = {}, []
    for key, row in views.items():
        view_path = Path(row['view'])
        view = json.loads(view_path.read_text())
        source = manifest[key]
        path = rollout_dir / key
        path.mkdir()
        task_id = source['task_id'] + '/' + source['variant']
        trajectory = view_to_atif(view)
        dump(path / 'trajectory.json', trajectory)
        dump(path / 'run.json', {'task_id': task_id, 'split': 'dev'})
        diff_text = view['final']['diff']
        if diff_text.startswith('[This snapshot is captured after the harness restores tests.'):
            if args.legacy_tasks is None:
                raise ValueError('legacy native replay requires --legacy-tasks preparation declarations')
            diff_text, receipt = without_preparation(
                diff_text, args.legacy_tasks / source['task_id'], source['variant'])
            dump(path / 'preparation-receipt.json', receipt)
        (path / 'final.diff').write_text(diff_text)
        (path / 'prompt.md').write_text(view['task_text'])
        native_rows.append({'id': key, 'task_id': task_id, 'path': str(path.resolve())})
        for artifact in path.iterdir():
            inputs[str(artifact.resolve())] = digest(artifact)
    native_manifest = args.out / 'manifest.jsonl'
    native_manifest.write_text(''.join(json.dumps(row) + '\n' for row in native_rows))
    cache = args.out / 'empty-autoreview.jsonl'
    cache.write_text('')
    station = args.out / 'empty-verdicts.jsonl'
    station.write_text('')
    base = json.loads(POLICY_SOURCE.read_text())
    if base['autoreview']['mode'] != 'cached':
        raise ValueError('reference replay no longer uses cached AutoReview')
    spec = {'schema': 'evallab.line_spec/v1', 'batch_id': 'ov1106-impscale', 'split': 'dev',
            'sources': [{'corpus': 'ov1106-impscale', 'rollouts': str(native_manifest.resolve())}],
            'task_status': None, 'task_action': base['task_action'],
            'task_policy': base.get('task_policy', {}),
            'autoreview': {'mode': 'cached', 'cache': str(cache.resolve()), 'max_usd': 0},
            'models': {'allowed': []}, 'audit': base['audit'],
            'judge': {'queue': str((args.out / 'judge-queue.jsonl').resolve()),
                      'write': False, 'verdicts': str(station.resolve())},
            'labels_policy': base['labels_policy'], 'out_root': str((args.out / 'batches').resolve())}
    spec_path = args.out / 'spec.json'
    dump(spec_path, spec)
    policy_copy = args.out / 'reference-policy.json'
    policy_copy.write_bytes(POLICY_SOURCE.read_bytes())
    for path in (native_manifest, cache, station, spec_path, policy_copy):
        inputs[str(path.resolve())] = digest(path)
    dump(args.out / 'input-hashes.json', inputs)
    print(json.dumps({'staged': len(native_rows), 'spec': str(spec_path),
                      'autoreview': 'missing readings remain native CACHE_MISS; no live calls'}))


def run(args):
    gate = gates(args.admitted, args.frozen)
    index = args.out / 'input-hashes.json'
    if gate['freeze']['inputs'].get(str(index.resolve())) != digest(index):
        raise ValueError('native line input index is not frozen')
    expected = json.loads((args.out / 'input-hashes.json').read_text())
    for name, sha in expected.items():
        if gate['freeze'].get('inputs', {}).get(name) != sha or digest(Path(name)) != sha:
            raise ValueError(f'unfrozen native line input: {name}')
    from evallab.line.core import run_line
    batch = run_line(args.out / 'spec.json', ROOT)
    output = []
    contract = json.loads(Path(__file__).with_name('contract.json').read_text())
    traces = {row['run_id']: row for row in rows(batch / 'trace.jsonl')}
    for row in rows(batch / 'decisions.jsonl'):
        output.append({'run_key': row['run_id'], 'version': contract['line_version'],
                       'decision': row['decision'], 'reasons': row['reasons'],
                       'autoreview': traces[row['run_id']]['autoreview'],
                       'batch': str(batch), 'decisions_sha256': digest(batch / 'decisions.jsonl')})
    destination = args.out / 'line-decisions.jsonl'
    destination.write_text(''.join(json.dumps(row) + '\n' for row in output))
    print(json.dumps({'batch': str(batch), 'decisions': str(destination), 'rows': len(output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--legacy-tasks', type=Path,
                        help='declared legacy task packages; required for post-restoration snapshots')
    parser.add_argument('mode', choices=('prepare', 'run'))
    parser.add_argument('--admitted', type=Path, default=LANE / 'ADMITTED.json')
    parser.add_argument('--frozen', type=Path, default=LANE / 'FROZEN.json')
    parser.add_argument('--runs', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare' and args.runs is None:
        parser.error('prepare requires --runs')
    (prepare if args.mode == 'prepare' else run)(args)


if __name__ == '__main__':
    main()
