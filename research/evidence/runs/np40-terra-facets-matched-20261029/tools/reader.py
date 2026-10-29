#!/usr/bin/env python3
"""NP40 private, resumable Atlas reader. No label data is loaded here."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from evallab.production import run_atlas as atlas
from evallab.production.corpus_queue import CorpusRegistry
from evallab.production.trace_harness_stream import OVERLAY, attempt_receipt, is_refusal
from evallab.readers.omp_model import OmpRouteError, parse_omp_stream, render_prompt, subscription_env
from evallab.seeker.lanes import codex_weekly_used

ROOT = Path.home() / '.local/state/evallab-prod-live/trace-harness/np40'
SPEND = Path.home() / '.local/state/evallab-failure-library/spend.jsonl'
TERRA = 'openai-codex/gpt-6.1-terra'
SOL = 'openai-codex/gpt-6.1-sol'
CAP = 600
BATCH = 8


def now():
    return datetime.now(UTC).isoformat(timespec='seconds')


def append(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def load(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def digest(ids):
    return hashlib.sha256(''.join(x + '\n' for x in ids).encode()).hexdigest()


def forbidden_events(stdout):
    """Reject any fallback marker or tool-use evidence in structured events."""
    fallback = False
    tool = False
    def walk(x):
        nonlocal fallback, tool
        if isinstance(x, dict):
            kind = str(x.get('type', '')).lower().replace('-', '_')
            if 'fallback' in kind or kind == 'model_changed' or x.get('resolvedModelIsFallback'):
                fallback = True
            if kind in {'toolcall', 'tool_call', 'tool_use', 'tooluse', 'tool_result', 'tool_execution_start', 'tool_execution_end'} or x.get('role') == 'tool' or x.get('tool_calls'):
                tool = True
            for value in x.values():
                walk(value)
        elif isinstance(x, list):
            for value in x:
                walk(value)
    for line in stdout.splitlines():
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return fallback, tool


def verified(stdout, model):
    fallback, tools = forbidden_events(stdout)
    if fallback or tools:
        raise OmpRouteError(f'fallback={fallback}, tool_use={tools}')
    return parse_omp_stream(stdout, model)


def ids_file(path, count):
    ids = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if len(ids) != count or len(set(ids)) != count:
        raise ValueError(f'{path}: expected {count} distinct run IDs')
    return ids


class CallFailure(RuntimeError):
    """This run's call was rejected; other runs may proceed under the guard."""


class Reader:
    def __init__(self, root):
        self.root = root
        self.receipts = root / 'receipts.jsonl'
        self.rows = load(self.receipts)
        starts = {r['call_id'] for r in self.rows if r.get('event') == 'dispatch'}
        ends = {r['call_id'] for r in self.rows if r.get('event') == 'result'}
        if starts != ends:
            raise RuntimeError('Unfinished dispatch receipt: inspect its private stream and account manually; no new calls allowed')
        self.results = {r['call_id']: r for r in self.rows if r.get('event') == 'result'}
        logged = {r.get('call_id') for r in load(SPEND) if r.get('purpose') == 'NP40-TT-1'}
        for row in self.results.values():
            if row['call_id'] not in logged:
                append(SPEND, {'ts': row['ts'], 'route': row['model'], 'usd': 0, 'purpose': 'NP40-TT-1', 'calls': 1, 'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens'], 'call_id': row['call_id']})
        self.parse_failure_ids = {r['call_id'] for r in load(root / 'parse-failures.jsonl')}
        self.used = 2 + len(starts)  # retained Terra and Sol canaries
        if self.used > CAP:
            raise RuntimeError('call cap exceeded in existing receipts')
        self.since_quota = BATCH
        self.consecutive_refusals = 0
        self.consecutive_failures = 0
        for row in self.rows:
            if row.get('event') != 'result':
                continue
            if row['ok']:
                self.consecutive_refusals = self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                self.consecutive_refusals = self.consecutive_refusals + 1 if row.get('refusal') else 0

    def complete(self, run_id, model, stage, attempt, messages):
        system, prompt = render_prompt(messages)
        call_id = hashlib.sha256(json.dumps([run_id, model, stage, attempt, messages], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        existing = self.results.get(call_id)
        if existing:
            if not existing['ok']:
                raise CallFailure(f'prior failed call {call_id}; not retried')
            reply = verified((self.root / 'streams' / (call_id + '.jsonl')).read_text(), model)
            return reply.text, existing
        if self.consecutive_refusals >= 3 or self.consecutive_failures >= 3:
            raise RuntimeError('three consecutive refusals or failed calls; stopped')
        if self.used >= CAP:
            raise RuntimeError('600-call cap reached')
        if self.since_quota >= BATCH:
            used = codex_weekly_used()
            append(self.root / 'quota.jsonl', {'ts': now(), 'seven_day_used_fraction': used, 'before_call_number': self.used + 1})
            if used is None or used >= 0.85:
                raise RuntimeError(f'Codex 7-day quota unavailable or >=85%: {used}')
            self.since_quota = 0
        stream = self.root / 'streams' / (call_id + '.jsonl')
        stream.parent.mkdir(parents=True, exist_ok=True)
        append(self.receipts, {'event': 'dispatch', 'ts': now(), 'call_id': call_id, 'run_id': run_id, 'model': model, 'stage': stage, 'attempt': attempt})
        self.used += 1
        self.since_quota += 1
        start = time.monotonic()
        stdout = ''
        error = None
        seen = {}
        reply = None
        try:
            with tempfile.TemporaryDirectory(prefix='np40-reader-') as work:
                overlay = Path(work) / 'overlay.yml'
                overlay.write_text(OVERLAY, encoding='utf-8')
                proc = subprocess.run(['omp', '-p', '--mode', 'json', '--no-session', '--no-tools', '--no-lsp', '--no-extensions', '--no-skills', '--no-rules', '--no-title', '--no-pty', '--config', str(overlay), '--model', model, '--system-prompt', system, '--thinking', 'low'], input=prompt, capture_output=True, text=True, timeout=600, cwd=work, env=subscription_env(os.environ))
            stdout = proc.stdout
            if proc.returncode:
                raise OmpRouteError(f'omp exit {proc.returncode}')
            reply = verified(stdout, model)
        except (OSError, subprocess.TimeoutExpired, OmpRouteError) as exc:
            if isinstance(exc, subprocess.TimeoutExpired) and exc.stdout:
                stdout = exc.stdout.decode(errors='replace') if isinstance(exc.stdout, bytes) else exc.stdout
            error = f'{type(exc).__name__}: {exc}'[:500]
        stream.write_text(stdout, encoding='utf-8')
        seen = attempt_receipt(stdout)
        fallback, tools = forbidden_events(stdout)
        refusal = is_refusal(error or '', seen)
        row = {'event': 'result', 'ts': now(), 'call_id': call_id, 'run_id': run_id, 'model': model, 'stage': stage, 'attempt': attempt, 'ok': reply is not None, 'error': error, 'served': seen.get('served'), 'fallback': fallback, 'tool_use': tools, 'refusal': refusal, 'stop_reason': seen.get('stop_reason'), 'input_tokens': seen.get('input_tokens', 0), 'output_tokens': seen.get('output_tokens', 0), 'elapsed_seconds': round(time.monotonic()-start, 3)}
        append(self.receipts, row)
        append(SPEND, {'ts': now(), 'route': model, 'usd': 0, 'purpose': 'NP40-TT-1', 'calls': 1, 'input_tokens': row['input_tokens'], 'output_tokens': row['output_tokens'], 'call_id': call_id})
        self.results[call_id] = row
        if reply is None:
            self.consecutive_failures += 1
            self.consecutive_refusals = self.consecutive_refusals + 1 if refusal else 0
            raise CallFailure(f'call failed: {error}; refusal={refusal}')
        self.consecutive_refusals = self.consecutive_failures = 0
        return reply.text, row

    def parse_failure(self, row):
        if row['call_id'] not in self.parse_failure_ids:
            append(self.root / 'parse-failures.jsonl', {'ts':now(), **row})
            self.parse_failure_ids.add(row['call_id'])

    def extract(self, run, model, pattern):
        blind = atlas.blind_run(run)
        count = len(blind.steps)
        if not count:
            raise ValueError('run has no steps')
        chunks = atlas.plan_chunks(atlas.render_segments(blind, pattern=pattern))
        used_rows = []
        if len(chunks) == 1:
            body = f'The complete run record follows ({count} steps, numbered 0 to {count - 1}).\n\n' + '\n\n'.join(s.text for s in chunks[0])
        else:
            notes = []
            for index, chunk in enumerate(chunks, 1):
                first, last = chunk[0].step, chunk[-1].step
                instructions = atlas.MAP_INSTRUCTIONS.format(index=index, total=len(chunks), first=first, last=last, count=count)
                messages = [{'role':'system','content':atlas.READER_SYSTEM}, {'role':'user','content':'\n\n'.join(s.text for s in chunk) + '\n\n' + instructions}]
                note = None
                for attempt in (1, 2):
                    text, row = self.complete(run.run_id, model, f'map:{index}/{len(chunks)}', attempt, messages)
                    used_rows.append(row)
                    try:
                        note = atlas._json_object(text)
                    except ValueError:
                        note = None
                    if isinstance(note, dict):
                        break
                    self.parse_failure({'call_id':row['call_id'], 'run_id':run.run_id, 'model':model, 'stage':'map'})
                    messages += [{'role':'assistant','content':text}, {'role':'user','content':'Return the notes as one JSON object only.'}]
                if not isinstance(note, dict):
                    raise ValueError(f'part {index} notes were not a JSON object')
                lines = [f'Part {index} of {len(chunks)} (steps {first}-{last}):']
                if isinstance(note.get('task'), str) and note['task'].strip():
                    lines.append(f"Task: {note['task'].strip()}")
                lines.append(f"Summary: {str(note.get('summary') or '').strip()}")
                in_chunk = {s.step for s in chunk}
                for item in note.get('notable') or []:
                    if isinstance(item, dict) and isinstance(item.get('step'), int) and item['step'] in in_chunk:
                        lines.append(f"- [step {item['step']}] {str(item.get('note') or '').strip()}")
                notes.append('\n'.join(lines))
            body = (f'The run record ({count} steps, numbered 0 to {count - 1}) was too long to show at once, so it was read in {len(chunks)} consecutive parts. The notes from each part follow in order.\n\n' + atlas.redact_sources('\n\n'.join(notes), pattern))
        messages = [{'role':'system','content':atlas.READER_SYSTEM}, {'role':'user','content':body + '\n\n' + atlas.FORM_INSTRUCTIONS}]
        form = None
        for attempt in (1, 2):
            text, row = self.complete(run.run_id, model, 'form', attempt, messages)
            used_rows.append(row)
            form, errors = atlas.parse_form(text, step_count=count)
            if form is not None:
                break
            self.parse_failure({'call_id':row['call_id'], 'run_id':run.run_id, 'model':model, 'stage':'form', 'errors':errors})
            shorten = [name for name in atlas.WORD_LIMITS if any(e.startswith(f'{name} has') for e in errors)]
            advice = ''.join(f' Rewrite {name} in at most {atlas.WORD_LIMITS[name] - 5} words.' for name in shorten)
            messages += [{'role':'assistant','content':text}, {'role':'user','content':'The form is invalid: ' + '; '.join(errors) + '.' + advice + ' Return the corrected JSON object only.'}]
        if form is None:
            raise ValueError('invalid form after one retry: ' + '; '.join(errors))
        record = {'run_id':run.run_id, 'source':run.source, 'split':run.split, 'facet_version':atlas.FACET_VERSION, 'facet_model':model, 'input_tokens':sum(r['input_tokens'] for r in used_rows), 'output_tokens':sum(r['output_tokens'] for r in used_rows), 'goal':form['goal'].strip(), 'approach':form['approach'].strip(), 'outcome':'unknown', 'tests':form['tests'], **{n:form[n] for n in atlas.BOOLEAN_FIELDS}, 'eval_awareness':form['eval_awareness'], 'shortcut':form['shortcut'].strip(), 'suspicion':form['suspicion'], 'evidence_steps':[blind.origins[s] for s in form['evidence_steps']]}
        record = {name:record[name] for name in atlas.FACET_FIELDS}
        problems = atlas.validate_record(record, step_ids=blind.origins)
        if problems:
            raise ValueError('invalid record: ' + '; '.join(problems))
        return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--terra-ids', type=Path, required=True)
    ap.add_argument('--sol-ids', type=Path, required=True)
    ap.add_argument('--state', type=Path, default=ROOT)
    ap.add_argument('--database', type=Path, default=atlas.DEFAULT_DATABASE)
    args = ap.parse_args()
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.state, 0o700)
    terra, sol = ids_file(args.terra_ids, 246), ids_file(args.sol_ids, 40)
    if not set(sol) <= set(terra):
        raise ValueError('Sol IDs must be a subset of Terra IDs')
    for model, name in ((TERRA, 'canary-terra.stdout.jsonl'), (SOL, 'canary-sol.stdout.jsonl')):
        verified((args.state / name).read_text(), model)
    registry = CorpusRegistry()
    conn = atlas.connect_readonly(args.database)
    try:
        admitted = atlas.admitted_task_ids(conn, registry)
        # Same admitted goodfire-dev selector as trace_harness.resolve_runs, without reading rewards or labels.
        from evallab.production.trace_harness import GOODFIRE_DATASET
        gated = set(atlas.cloud_trace_ids(conn, admitted=admitted, dataset_ids=[GOODFIRE_DATASET], trace_ids=terra))
        if gated != set(terra):
            raise RuntimeError(f'strict goodfire-dev admission gate: {len(gated)}/246')
        pattern = atlas.database_source_pattern(args.database)
        records = load(args.state / 'facets.jsonl')
        done = {(r['run_id'], r['facet_model']) for r in records}
        expected = {(x, TERRA) for x in terra} | {(x, SOL) for x in sol}
        if len(done) != len(records) or not done <= expected:
            raise RuntimeError('duplicate or unexpected facet rows; inspect private store')
        marker = args.state / 'study-complete.json'
        if marker.exists():
            existing = json.loads(marker.read_text())
            if done == expected and existing.get('models', {}).get(TERRA, {}).get('ids_sha256') == digest(terra) and existing.get('models', {}).get(SOL, {}).get('ids_sha256') == digest(sol):
                print(json.dumps(existing))
                return
            raise RuntimeError('existing completion marker does not match current selectors and facets')
        reader = Reader(args.state)
        for model, ids in ((TERRA, terra), (SOL, sol)):
            for run_id in ids:
                if (run_id, model) in done:
                    continue
                row = conn.execute('SELECT c.dataset_id, c.split FROM agent_traces a JOIN corpus_tasks c USING(task_id) WHERE a.trace_id = ?', (run_id,)).fetchone()
                if row is None:
                    raise RuntimeError('gated trace disappeared')
                run = atlas.AtlasRun(run_id, row['dataset_id'], str(row['split']).upper(), 'unknown', atlas.cloud_doc(conn, run_id))
                try:
                    record = reader.extract(run, model, pattern)
                except CallFailure as exc:
                    print(f'Rejected {model} run {run_id}: {exc}', file=sys.stderr)
                    continue
                append(args.state / 'facets.jsonl', record)
                done.add((run_id, model))
        if done != expected:
            raise RuntimeError('incomplete facets')
        after = codex_weekly_used()
        append(args.state / 'quota.jsonl', {'ts':now(), 'seven_day_used_fraction':after, 'phase':'after'})
        if after is None:
            raise RuntimeError('Codex 7-day quota after reading unavailable; completion withheld')
        result = {'complete':True, 'gate_count':len(gated), 'models':{TERRA:{'count':len(terra), 'ids_sha256':digest(terra)}, SOL:{'count':len(sol), 'ids_sha256':digest(sol)}}, 'calls_total_including_canaries':reader.used, 'completed_at':now(), 'quota_after':after}
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2) + '\n')
        os.replace(temporary, marker)
        print(json.dumps(result))
    finally:
        conn.close()


if __name__ == '__main__':
    os.umask(0o077)
    try:
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (ROOT / 'reader.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            main()
    except Exception as exc:
        print(f'NP40 reader stopped: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
