#!/usr/bin/env python3
"""Frozen DEV-only, cached-image defensive controls. No model, build, pull or injection.

prepare writes the manifest and probe bytes without scoring. run requires their
frozen SHA-256 receipt and executes Harbor's native verifier using the existing
LockedDockerEnvironment boundary. summarize never treats infrastructure as zero.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
SCRATCH = Path('/private/tmp/ov1104/wave2-grader/workers/smoke')
INVENTORY = Path.home() / 'Developer/eval-lab-results/2026-11-02/A1-INVENTORY/gold-universe.json'
SCORECARD = REPO / 'research/experiments/ov1104-grader/scorecard/scorecard.json'
SELECTION = REPO / 'research/experiments/ov-sting-mutants-20261031/results/selection.json'
BACKEND = 'smoke_runner:CachedLockedEnvironment'


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def package_digest(root: Path) -> str:
    # Same identity algorithm as evallab.registry.task_directory_digest.
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name in {'.DS_Store', '.git', '__pycache__', '.pytest_cache'} or path.suffix in {'.pyc', '.pyo', '.tmp'}:
            continue
        if any(part.startswith('heldout-') for part in path.parts):
            raise ValueError('excluded heldout path in DEV package')
        digest.update(f'{sha(path)}  ./{path.relative_to(root).as_posix()}\n'.encode())
    return 'sha256:' + digest.hexdigest()


def docker_json(*args: str) -> object:
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def image_info(reference: str | None) -> dict | None:
    if not reference:
        return None
    try:
        item = docker_json('image', 'inspect', reference)[0]
    except (subprocess.SubprocessError, ValueError, OSError):
        return None
    return {key: item.get(key) for key in ('Id', 'RepoTags', 'RepoDigests', 'Architecture', 'Os')}


def grid_bytes(instruction: str) -> bytes:
    numbers = ' '.join(str(i) for i in range(1000)) + '\n'
    words = ' '.join(re.findall(r'[A-Za-z0-9_]+', instruction)) + '\n'
    return (numbers + words).encode('ascii')[:4096].ljust(4096, b' ')


def artifact_files(config: dict) -> list[str]:
    protected = {'tests', 'test', 'logs', 'solution', 'proc', 'sys', 'dev', 'etc'}
    paths = []
    for item in config.get('artifacts', []):
        path = item if isinstance(item, str) else item.get('source', '')
        parts = Path(path).parts
        if not path.startswith('/') or path.endswith('/') or '..' in parts or any(x.lower() in protected for x in parts):
            continue
        if isinstance(item, dict) and (item.get('service') not in (None, 'main') or item.get('exclude')):
            continue
        if Path(path).suffix.lower() not in {'.json', '.jsonl', '.csv', '.tsv', '.txt', '.xml', '.musicxml', '.step', '.xlsx', '.fcstd'}:
            continue
        if '/inputs/' in path:
            continue
        paths.append(path)
    return paths


def prepare() -> None:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    inventory = json.loads(INVENTORY.read_text())
    scorecard = json.loads(SCORECARD.read_text())
    selection = {x['task_id']: x for x in json.loads(SELECTION.read_text())['chosen']}
    admitted = {x['task_id']: (x['package_path'], 'A1-INVENTORY strictly_dev=true') for x in inventory['tasks'] if x['strictly_dev']}
    for row in scorecard:
        if row['task_id'] not in admitted:
            admitted[row['task_id']] = (selection[row['task_id']]['package'], 'prior scorecard strict-DEV + STING selection')
    result = subprocess.run(['docker', 'image', 'ls', '--digests', '--no-trunc', '--format', '{{json .}}'], capture_output=True, text=True, timeout=30)
    (SCRATCH / 'docker-images.stdout.jsonl').write_text(result.stdout)
    (SCRATCH / 'docker-images.stderr.txt').write_text(result.stderr)
    if result.returncode:
        raise RuntimeError('Docker enumeration failed; see retained stderr')
    rows = []
    for task_id, (package, admission) in sorted(admitted.items()):
        root = Path(package)
        config = tomllib.loads((root / 'task.toml').read_text())
        digest = package_digest(root)
        slug = root.name.lower().replace('_', '-')[:28]
        tag = f'qa-task/{slug}-env:{digest.split(":")[1][:12]}'
        tests_tag = f'qa-task/{slug}-tests:{digest.split(":")[1][:12]}'
        agent = image_info(tag) or image_info(config.get('environment', {}).get('docker_image'))
        separate = config.get('verifier', {}).get('environment_mode') == 'separate'
        tests_df = (root / 'tests/Dockerfile').exists()
        verifier = image_info(tests_tag) if separate and tests_df else agent
        grid = grid_bytes((root / 'instruction.md').read_text())
        probe = SCRATCH / 'probes' / (task_id + '.grid.txt')
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(grid)
        paths = artifact_files(config)
        environment = config.get('environment', {})
        unavailable = []
        if agent is None:
            unavailable.append('agent_image_unavailable')
        if verifier is None:
            unavailable.append('verifier_image_unavailable')
        if any((root / 'environment' / name).exists() for name in ('docker-compose.yaml', 'docker-compose.yml', 'compose.yaml', 'compose.yml')):
            unavailable.append('unsupported_compose_locked_backend')
        if environment.get('gpus', 0):
            unavailable.append('unsupported_accelerator')
        rows.append({
            'task_id': task_id, 'package': package, 'package_digest': digest,
            'admission': admission, 'agent_image': agent, 'verifier_image': verifier,
            'cached_environment_reference': tag, 'cached_verifier_reference': tests_tag,
            'source_image': environment.get('docker_image'), 'separate_verifier': separate,
            'resources': {key: environment.get(key) for key in ('cpus', 'memory_mb', 'storage_mb', 'gpus')},
            'null_supported': True, 'grid_paths': paths,
            'grid_interface_status': 'declared_inert_file_artifact' if paths else 'unsupported_no_inert_file_artifact',
            'grid_bytes': len(grid), 'grid_sha256': sha(probe), 'grid_file': str(probe),
            'availability': 'eligible' if not unavailable else 'unavailable', 'unavailable_reasons': unavailable,
        })
    manifest = {
        'schema': 'wave2.defensive-smoke.manifest/v1', 'created_at': datetime.now(UTC).isoformat(),
        'backend': BACKEND, 'timeout_seconds': 300, 'max_owned_containers': 1,
        'population': 'immutable admitted inventory plus prior strict-DEV scorecard extras; not all present-day DEV tasks',
        'admission_sources': [{'path': str(path), 'sha256': sha(path)} for path in (INVENTORY, SCORECARD, SELECTION)],
        'image_enumeration': {'path': str(SCRATCH / 'docker-images.stdout.jsonl'), 'sha256': sha(SCRATCH / 'docker-images.stdout.jsonl')},
        'exit0_probe': {'status': 'unexecuted_safety_exclusion', 'full_X07_complete': False},
        'tasks': rows,
    }
    save(HERE / 'manifest.json', manifest)
    print(json.dumps({'population': len(rows), 'availability': dict(Counter(x['availability'] for x in rows)), 'manifest_sha256': sha(HERE / 'manifest.json')}))


def load_frozen() -> dict:
    frozen = json.loads((HERE / 'freeze.json').read_text())
    for name, digest in frozen['files'].items():
        if sha(HERE / name) != digest:
            raise ValueError(f'frozen file changed: {name}')
    manifest = json.loads((HERE / 'manifest.json').read_text())
    for row in manifest['tasks']:
        if sha(Path(row['grid_file'])) != row['grid_sha256']:
            raise ValueError('frozen probe changed')
    return manifest


# Optional runtime imports stay outside prepare/summarize. This class is built
# only by the native Harbor runtime, never by a model or task-solving agent.
if os.environ.get('OV1104_SMOKE_RUNTIME') == '1':
    from evallab.harbor_docker import LockedDockerEnvironment

    class CachedLockedEnvironment(LockedDockerEnvironment):
        """Keep every official lock check; only replace image preparation with lookup."""

        def __init__(self, *, environment_dir: Path, task_env_config, **kwargs):
            manifest = load_frozen()
            parent = Path(environment_dir).resolve().parent
            row = next(x for x in manifest['tasks'] if Path(x['package']).resolve() == parent)
            verifier = Path(environment_dir).name == 'tests'
            image = row['verifier_image' if verifier else 'agent_image']
            if image is None or image_info(image['Id']) != image:
                # Inspect by ID reports the same recorded references on this daemon.
                current = image_info(image['Id']) if image else None
                if not current or current['Id'] != image['Id']:
                    raise ValueError('frozen local image unavailable')
            self._frozen_image_id = image['Id']
            task_env_config = task_env_config.model_copy(update={'docker_image': image['Id']})
            kwargs['mounts'] = []
            super().__init__(environment_dir=environment_dir, task_env_config=task_env_config, **kwargs)
            if self._mounts:
                raise ValueError('smoke forbids every host mount')

        @property
        def capabilities(self):
            # No bind mounts: make native Harbor download its own verifier logs.
            from harbor.environments.capabilities import EnvironmentCapabilities
            return EnvironmentCapabilities(disable_internet=True, mounted=False, dynamic_network_policy=True)

        def _build_verifier_image_instead_of_agent_image(self):
            # Native tests/Dockerfile image is already built, digest-bound above.
            self.task_env_config = self.task_env_config.model_copy(update={'docker_image': self._frozen_image_id})
            self._env_vars.prebuilt_image_name = self._frozen_image_id

        async def _run_docker_compose_command(self, command, **kwargs):
            if command[0] in {'build', 'pull', 'run'}:
                raise RuntimeError('smoke forbids image builds, pulls and extra containers')
            if command[0] == 'up':
                command = [*command, '--no-build', '--pull', 'never']
            return await super()._run_docker_compose_command(command, **kwargs)

        async def start(self, force_build=False):
            if force_build:
                raise ValueError('smoke forbids force_build')
            await super().start(force_build=False)
            container = await self._daemon_json('inspect', self._container_id)
            host = container[0]['HostConfig']
            expected_cpus = int(self.task_env_config.cpus * 1_000_000_000)
            expected_memory = int(self.task_env_config.memory_mb * 1024 * 1024)
            if host.get('NanoCpus') != expected_cpus or host.get('Memory') != expected_memory:
                raise ValueError('smoke refuses resource limits that differ from task declarations')
            if container[0].get('Mounts'):
                raise ValueError('smoke refuses every actual host mount')
            record = {'image_id': self._frozen_image_id, 'mounts': container[0]['Mounts'], 'network_lock_confirmed': self._lock_confirmed,
                      'NanoCpus': host['NanoCpus'], 'Memory': host['Memory'], 'declared_cpus': self.task_env_config.cpus,
                      'declared_memory_mb': self.task_env_config.memory_mb}
            save(self.trial_paths.trial_dir / ('smoke-isolation-' + self.session_id + '.json'), record)


async def execute(row: dict, probe: str, out: Path, timeout: int) -> dict:
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    from evallab.docker_lock import docker_slot
    if package_digest(Path(row['package'])) != row['package_digest']:
        raise ValueError('package changed after pre-registration')
    script = None
    if probe == 'grid':
        payload = base64.b64encode(Path(row['grid_file']).read_bytes()).decode()
        # Writes inert data only, to explicit preregistered artifact paths.
        import shlex
        lines = ['set -eu']
        for target in row['grid_paths']:
            q = shlex.quote(target)
            lines += [f'test ! -L {q}', f'mkdir -p {shlex.quote(str(Path(target).parent))}', f'printf %s {shlex.quote(payload)} | base64 -d > {q}']
        script = out / 'grid-write.sh'
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text('\n'.join(lines) + '\n')
    config = TrialConfig.model_validate({
        'task': {'path': row['package']}, 'trial_name': 'trial', 'trials_dir': out,
        'agent': {'import_path': 'evallab.qa.task_modal:QAScriptAgent', 'override_timeout_sec': timeout,
                  'kwargs': {'qa_script_path': str(script) if script else None, 'qa_command': 'mkdir -p /logs/agent && bash /tmp/qa-shortcut.sh' if script else None,
                             'qa_workdir': None, 'qa_timeout_seconds': timeout}},
        'environment': {'import_path': BACKEND, 'delete': True, 'cpu_enforcement_policy': 'limit', 'memory_enforcement_policy': 'limit',
                        'kwargs': {'egress_lock': True, 'mounts': []}},
    })
    with docker_slot(label='ov1104-defensive-smoke/' + row['task_id'], slots=2, timeout=1800):
        trial = await Trial.create(config)
        try:
            result = await asyncio.wait_for(trial.run(), timeout=timeout)
            if probe == 'grid' and not result.exception_info:
                for target in row['grid_paths']:
                    artifact = out / 'trial/artifacts' / target.lstrip('/')
                    if not artifact.is_file() or sha(artifact) != row['grid_sha256']:
                        raise ValueError('grid artifact missing or differs from frozen 4096-byte payload')
            return result.model_dump(mode='json')
        finally:
            # Native Trial owns cleanup, including separate verifier teardown.
            if getattr(trial, '_environment', None) is not None:
                await trial._environment.stop(delete=True)


def run(task: str | None) -> None:
    if os.environ.get('OV1104_SMOKE_RUNTIME') != '1':
        raise ValueError('set OV1104_SMOKE_RUNTIME=1 for native execution')
    manifest = load_frozen()
    targets = [x for x in manifest['tasks'] if x['availability'] == 'eligible' and (task is None or x['task_id'] == task)]
    for row in targets:
        for probe in ['null', *(['grid'] if row['grid_paths'] else [])]:
            out = SCRATCH / ('runs-amendment3' if probe == 'grid' else 'runs-amendment2') / row['task_id'] / probe
            if (out / 'observation.json').exists():
                continue
            out.mkdir(parents=True, exist_ok=True)
            observation = {'task_id': row['task_id'], 'probe': probe, 'started_at': datetime.now(UTC).isoformat(),
                           'backend': BACKEND, 'status': 'infrastructure_error', 'reward': None}
            try:
                result = asyncio.run(execute(row, probe, out, manifest['timeout_seconds']))
                save(out / 'native-result.json', result)
                exception = result.get('exception_info')
                rewards = (result.get('verifier_result') or {}).get('rewards') or {}
                reward = rewards.get('reward')
                if isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward) and not exception:
                    observation.update(status='evaluated', reward=reward)
                else:
                    observation['error'] = exception or 'native verifier produced no finite scalar reward'
            except Exception as exc:
                observation['error'] = f'{type(exc).__name__}: {exc}'
            observation['finished_at'] = datetime.now(UTC).isoformat()
            save(out / 'observation.json', observation)
            print(json.dumps(observation), flush=True)


def wilson(k: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.959963984540054
    p = k / n
    d = 1 + z*z/n
    center = (p + z*z/(2*n))/d
    radius = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return [max(0.0, center-radius), min(1.0, center+radius)]


def summarize() -> None:
    manifest = load_frozen()
    observations = []
    for row in manifest['tasks']:
        for probe in ['null', 'grid']:
            path = SCRATCH / ('runs-amendment3' if probe == 'grid' else 'runs-amendment2') / row['task_id'] / probe / 'observation.json'
            if path.exists():
                item = json.loads(path.read_text())
                item.update(receipt=str(path), receipt_sha256=sha(path))
            else:
                item = {'task_id': row['task_id'], 'probe': probe, 'reward': None,
                        'status': 'unsupported_interface' if probe == 'grid' and not row['grid_paths'] else 'unavailable_image' if row['availability'] != 'eligible' else 'not_executed'}
            observations.append(item)
    mutation = {}
    for row in json.loads(SCORECARD.read_text()):
        block = row.get('mutants_original')
        if block and block['total']:
            mutation[row['task_id']] = block['killed'] < block['total']
    evaluated = {x['task_id'] for x in observations if x['status'] == 'evaluated'}
    weak = {x['task_id'] for x in observations if x['status'] == 'evaluated' and x['reward'] == 1}
    null_eval = [x for x in observations if x['probe'] == 'null' and x['status'] == 'evaluated']
    grid_eval = [x for x in observations if x['probe'] == 'grid' and x['status'] == 'evaluated']
    both = {x['task_id'] for x in null_eval} & {x['task_id'] for x in grid_eval}
    joint = sorted(evaluated & mutation.keys())
    cells = Counter((task in weak, mutation[task]) for task in joint)
    summary = {
        'schema': 'wave2.defensive-smoke.summary/v1', 'manifest_sha256': sha(HERE / 'manifest.json'),
        'population_n': len(manifest['tasks']), 'availability': dict(Counter(x['availability'] for x in manifest['tasks'])),
        'null': {'evaluated_n': len(null_eval), 'weak_k': sum(x['reward'] == 1 for x in null_eval), 'wilson95': wilson(sum(x['reward'] == 1 for x in null_eval), len(null_eval))},
        'grid': {'interface_supported_n': sum(bool(x['grid_paths']) for x in manifest['tasks']), 'evaluated_n': len(grid_eval), 'weak_k': sum(x['reward'] == 1 for x in grid_eval), 'wilson95': wilson(sum(x['reward'] == 1 for x in grid_eval), len(grid_eval))},
        'any_evaluated_subset': {'evaluated_n': len(evaluated), 'weak_k': len(weak), 'wilson95': wilson(len(weak), len(evaluated)), 'not_complete_case': True},
        'two_supported_probes_complete_case': {'n': len(both), 'weak_k': len(both & weak), 'wilson95': wilson(len(both & weak), len(both))},
        'observations_by_status': dict(Counter(x['status'] for x in observations)),
        'prior_mutation_audit': {'n': len(mutation), 'weak_k': sum(mutation.values())},
        'joint_denominator': {'n': len(joint), 'task_ids': joint, 'cells': [{'smoke_weak': a, 'mutation_weak': b, 'n': cells[a,b]} for a in (False,True) for b in (False,True)]},
        'exit0': 'not_implemented_or_executed_safety_exclusion', 'full_X07_complete': False,
        'raw_receipts': {'null': str(SCRATCH / 'runs-amendment2'), 'grid': str(SCRATCH / 'runs-amendment3')}, 'observations': observations,
    }
    save(HERE / 'summary.json', summary)
    print(json.dumps({key:value for key,value in summary.items() if key != 'observations'}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'freeze', 'run', 'summarize'])
    parser.add_argument('--task')
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'freeze':
        save(HERE / 'freeze.json', {'created_at': datetime.now(UTC).isoformat(), 'files': {name:sha(HERE/name) for name in ('PREREG.md', 'manifest.json', 'smoke_runner.py')}})
        print(sha(HERE / 'freeze.json'))
    elif args.action == 'run':
        run(args.task)
    else:
        summarize()


if __name__ == '__main__':
    main()
