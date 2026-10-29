"""Behavioral gates for once-only OQ-8 dispatch and session attestation."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / 'research/experiments/oq8-data-access-ab/ab_driver.py'
spec = importlib.util.spec_from_file_location('oq8_driver', PATH)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


@pytest.fixture(scope='module')
def h():
    return d.helpers()


def session(tmp_path, messages):
    folder = tmp_path / 'sessions'
    folder.mkdir()
    (folder / 'one.jsonl').write_text(''.join(json.dumps({'type': 'message', 'message': m}) + '\n' for m in messages))


def assistant(model='gpt-6.1-terra', content=None, fallback=False):
    return {'role': 'assistant', 'provider': 'openai-codex', 'model': model,
            'resolvedModelIsFallback': fallback, 'stopReason': 'stop',
            'content': content or [{'type': 'text', 'text': '{"findings": []}'}],
            'usage': {'input': 10, 'cacheRead': 20, 'cacheWrite': 3, 'output': 7, 'reasoningTokens': 4}}


def test_arm_removes_only_retrieved_block(tmp_path):
    raw = b'prefix\nUNTRUSTED HISTORICAL CONTEXT:\n{"retrieved":"data"}\n\nUNAVAILABLE SOURCE FILES (omission is not a defect):\n[]\nTASK SOURCE:\nexact'
    path = tmp_path / 'B.txt'
    path.write_bytes(raw)
    case = {'prompts': {'B': {'path': str(path), 'sha256': d.sha(raw)}}}
    assert d.arm_prompt(case, 'A').encode() == raw
    assert d.arm_prompt(case, 'B') == 'prefix\nUNAVAILABLE SOURCE FILES (omission is not a defect):\n[]\nTASK SOURCE:\nexact'
    path.write_text('changed')
    with pytest.raises(ValueError, match='drift'):
        d.arm_prompt(case, 'A')


@pytest.mark.parametrize('change', ['fallback', 'wrong_model', 'missing_identity'])
def test_every_assistant_must_attest(tmp_path, h, change):
    first = assistant()
    if change == 'fallback':
        first['resolvedModelIsFallback'] = True
    elif change == 'wrong_model':
        first['model'] = 'gpt-6.1-sol'
    else:
        del first['provider']
    session(tmp_path, [first, assistant()])
    receipt = d.session_receipt(tmp_path, d.AUDITOR, 'B', h)
    assert receipt['status'] == 'invalid'


def test_tokens_include_cache_and_not_double_reasoning(tmp_path, h):
    session(tmp_path, [assistant(), assistant()])
    receipt = d.session_receipt(tmp_path, d.AUDITOR, 'A', h)
    assert receipt['status'] == 'answered'
    assert (receipt['tokens_in'], receipt['tokens_out']) == (66, 14)
    assert receipt['assistant_records'] == 2


def test_missing_accounting_stays_unknown(tmp_path, h):
    msg = assistant()
    del msg['usage']['cacheWrite']
    session(tmp_path, [msg])
    assert d.session_receipt(tmp_path, d.AUDITOR, 'A', h)['tokens_in'] is None


@pytest.mark.parametrize('arm,name,count,violates', [('B', 'qa_proxy', 8, False), ('B', 'qa_proxy', 9, True),
                                                   ('B', 'bash', 1, True), ('A', 'qa_proxy', 1, True)])
def test_only_proxy_and_eight_calls(tmp_path, h, arm, name, count, violates):
    calls = [{'type': 'toolCall', 'id': str(i), 'name': name, 'arguments': {'args': ['inventory']}} for i in range(count)]
    session(tmp_path, [assistant(content=calls), assistant()])
    receipt = d.session_receipt(tmp_path, d.AUDITOR, arm, h)
    assert (receipt['status'] == 'invalid') == violates
    assert bool(receipt['protocol_violations']) == violates


def test_consumed_session_never_dispatches(tmp_path, h, monkeypatch):
    session(tmp_path, [assistant()])
    monkeypatch.setattr(d, 'admit', lambda *a: pytest.fail('must not dispatch'))
    result = d.call(tmp_path, route=d.AUDITOR, prompt='unused', system='unused', arm='A', case={}, stop=tmp_path / 'STOP', h=h)
    assert result['status'] == 'answered'
    assert (tmp_path / 'receipt.json').exists()


def test_interrupted_consumed_marker_never_dispatches(tmp_path, h, monkeypatch):
    (tmp_path / 'consumed.json').write_text('{}')
    monkeypatch.setattr(d, 'admit', lambda *a: pytest.fail('must not dispatch'))
    result = d.call(tmp_path, route=d.AUDITOR, prompt='unused', system='unused', arm='A', case={}, stop=tmp_path / 'STOP', h=h)
    assert result['status'] == 'unanswered'
    assert result['tokens_in'] is None


def test_stop_blocks_before_quota_command(tmp_path, monkeypatch):
    stop = tmp_path / 'STOP'
    stop.touch()
    monkeypatch.setattr(d.subprocess, 'run', lambda *a, **k: pytest.fail('no subprocess permitted'))
    with pytest.raises(RuntimeError, match='stop file'):
        d.admit(stop, tmp_path)


@pytest.mark.parametrize('fraction,blocked', [(.8499, False), (.85, True), (.99, True)])
def test_weekly_threshold(tmp_path, monkeypatch, fraction, blocked):
    value = {'reports': [{'provider': 'openai-codex', 'limits': [{'window': {'id': '7d'}, 'amount': {'usedFraction': fraction}}]}]}
    monkeypatch.setattr(d.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(value)))
    if blocked:
        with pytest.raises(RuntimeError, match='85%'):
            d.admit(tmp_path / 'STOP', tmp_path)
    else:
        d.admit(tmp_path / 'STOP', tmp_path)


def test_unavailable_quota_fails_closed():
    with pytest.raises(RuntimeError, match='unavailable'):
        d.weekly_fraction({'reports': []})


def test_extension_enforces_cap_without_a_shell(tmp_path, monkeypatch):
    proxy = tmp_path / 'proxy.py'
    count = tmp_path / 'executions'
    proxy.write_text(f'import json, sys\nfrom pathlib import Path\np=Path({str(count)!r})\np.write_text(p.read_text()+\"x\" if p.exists() else \"x\")\nPath({str(tmp_path / "argv.json")!r}).write_text(json.dumps(sys.argv[1:]))\nprint(\"ok\")\n')
    monkeypatch.setattr(d, 'PROXY', proxy)
    tool_path = tmp_path / 'tool.mjs'
    case = {'case_id': 'D0001', 'stage': 'dev', 'task_id': 'fixed-task', 'item_id': 'fixed/item'}
    tool_path.write_text(d.extension(case, tmp_path))
    script = '''
import setup from TOOL;
let tool;
setup({registerTool: t => tool = t});
for (let i=0;i<8;i++) {
 const r=await tool.execute(String(i), {args:["inventory"]});
 if (r.isError) throw Error(JSON.stringify(r));
}
const ninth=await tool.execute("9", {args:["inventory"]});
if (!ninth.isError) throw Error("ninth executed");
'''.replace('TOOL', json.dumps(str(tool_path)))
    d.subprocess.run(['node', '--input-type=module', '-e', script], check=True, capture_output=True, text=True)
    assert count.read_text() == 'x' * 8
    argv = json.loads((tmp_path / 'argv.json').read_text())
    assert argv == ['--target-task', 'fixed-task', '--target-item', 'fixed/item',
                    '--counter', str(tmp_path / 'proxy.count'),
                    '--log', str(tmp_path / 'proxy.jsonl'), '--', 'inventory']


def smoke_cells():
    return [{'case_id': c, 'arm': a, 'paired': {'complete': True},
             'auditor': {'status': 'answered', 'fallback': False, 'protocol_violations': 0,
                         'tool_calls': [{'name': 'qa_proxy'}] if a == 'B' else []},
             'judge': {'status': 'not_required', 'fallback': False}}
            for c in ('D0001', 'D0003', 'D0005') for a in 'AB']


@pytest.mark.parametrize('failure', ['missing', 'invalid', 'fallback', 'protocol', 'no_proxy', 'quota'])
def test_smoke_gate_fails_closed(failure):
    cells = smoke_cells()
    assert d.smoke_gate(cells, .55)
    quota = .55
    if failure == 'missing':
        cells.pop()
    elif failure == 'invalid':
        cells[0]['paired']['complete'] = False
    elif failure == 'fallback':
        cells[0]['judge']['fallback'] = True
    elif failure == 'protocol':
        cells[0]['auditor']['protocol_violations'] = 1
    elif failure == 'no_proxy':
        cells[1]['auditor']['tool_calls'] = []
    else:
        quota = .85
    assert not d.smoke_gate(cells, quota)


def test_dispatch_stops_and_records_finished_cells(tmp_path, monkeypatch):
    monkeypatch.setattr(d, 'ROOT', tmp_path)
    seen = []
    def execute(case, arm, *args):
        seen.append(case['case_id'])
        raise d.AdmissionStopped('weekly quota reached 85%')
    monkeypatch.setattr(d, 'execute_cell', execute)
    jobs = [({'case_id': str(i), 'stage': 'dev'}, 'A') for i in range(4)]
    cells, stopped = d.dispatch(jobs, 1, tmp_path / 'STOP', {}, 'freeze', 'full')
    assert seen == ['0']
    assert cells == []
    receipt = json.loads((tmp_path / 'X/progress.json').read_text())
    assert receipt['cells_done'] == 0
    assert '85%' in receipt['stopped'] == stopped
