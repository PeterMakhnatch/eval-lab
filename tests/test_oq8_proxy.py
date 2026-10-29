import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('oq8_proxy', ROOT / 'research/experiments/oq8-data-access-ab/qa_proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class Registry:
    @staticmethod
    def task_name(value):
        return value.rsplit('/', 1)[-1].split('@')[0].lower()

    def items(self):
        return [SimpleNamespace(item_id='dataset/item', task_id='namespace/target')]


def test_target_task_is_projected_and_paths_withheld():
    result = {'ok': True, 'data': {'task_id': 'other/TARGET@v2', 'instruction': 'task', 'issues': [{'status': 'confirmed', 'label': 'leak'}], 'packages': {'0:original_package': {'withheld': ['research/answer-keys/key.json'], 'contents': [{'relative_path': 'tests/test.sh', 'text': 'test'}]}, '0:repaired_package': {'contents': ['answer']}}, 'runs': {'total': 1, 'list': [{'trace_id': 'r', 'outcome': 'fail', 'suspicion': 3, 'tests': 'modified'}]}}}
    out = proxy.mask(result, 'task', Registry(), 'target', 'dataset/item')
    assert out['data']['instruction'] == 'task'
    assert out['data']['packages']['0:original_package']['contents'][0]['text'] == 'test'
    assert set(out['data']['packages']) == {'0:original_package'}
    assert out['data']['runs']['list'] == [{'trace_id': 'r', 'outcome': 'fail'}]
    assert 'confirmed' not in json.dumps(out) and 'answer-keys' not in json.dumps(out)


def test_history_filters_aliases_but_keeps_other_examples():
    result = {'ok': True, 'data': {'similar_issues': [{'task_id': 'foo/TARGET@v3', 'issue_id': 'private'}, {'item_id': 'dataset/item', 'issue_id': 'also-private'}, {'task_id': 'other', 'issue_id': 'public'}]}}
    assert proxy.mask(result, 'history', Registry(), 'target', 'dataset/item')['data']['similar_issues'] == [{'task_id': 'other', 'issue_id': 'public'}]


def test_call_cap_and_uniform_refusal(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(proxy, 'invoke', lambda args, argv: calls.append(argv) or proxy.REFUSAL)
    monkeypatch.setattr(proxy.sys, 'argv', ['proxy', '--target-task', 'x', '--target-item', 'y', '--counter', str(tmp_path / 'count'), '--log', str(tmp_path / 'calls'), '--', 'history', 'x'])
    replies = []
    for _ in range(9):
        assert proxy.main() == 2
        replies.append(json.loads(capsys.readouterr().out))
    assert len(calls) == 8
    assert all(reply == proxy.REFUSAL for reply in replies)
    rows = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    assert len(rows) == 9 and all(row['return_tokens'] == proxy.tokens(proxy.REFUSAL) for row in rows)


def test_response_cap(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(proxy, 'invoke', lambda args, argv: {'ok': True, 'data': {'text': 'x' * 3000}})
    monkeypatch.setattr(proxy.sys, 'argv', ['proxy', '--target-task', 'x', '--target-item', 'y', '--counter', str(tmp_path / 'count'), '--log', str(tmp_path / 'calls'), '--token-cap', '200', '--', 'inventory'])
    assert proxy.main() == 2
    assert json.loads(capsys.readouterr().out) == proxy.REFUSAL
