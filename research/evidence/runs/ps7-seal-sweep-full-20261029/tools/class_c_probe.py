"""Class (c) review of the `task.instruction` hits: characterise a hit without printing any text (booleans and lengths only)."""
import argparse
import json
import re
from pathlib import Path

from evallab.production.cloud_trace_db import _sealed_pattern
from evallab.production.qa_data import Budget, _dispatch, open_context, register_parser

_DICT = Path('/usr/share/dict/words')
WORDS = {w.strip().lower() for w in _DICT.read_text(errors='replace').splitlines()} if _DICT.exists() else None
DB = '/private/tmp/ps2/store.sqlite'
parser = argparse.ArgumentParser()
register_parser(parser.add_subparsers(dest='command', required=True))
ctx = open_context(DB, budget=100000)
with ctx.database._transaction():
    admitted, seals = ctx.retriever._admitted_ids()
pat = _sealed_pattern(frozenset(seals))
seals = [s for s in seals if s]
out = []
for t in sorted(admitted):
    ctx.budget = Budget(99746)
    data = _dispatch('task', ctx, parser.parse_args(['qa-data', 'task', t]))
    ins = data.get('instruction') if isinstance(data, dict) else None
    if isinstance(ins, str) and pat.search(ins.casefold()):
        text = ins.casefold()
        rec = []
        for s in seals:
            pos = text.find(s)
            while pos >= 0:
                before = text[pos - 1] if pos > 0 else ' '
                end = pos + len(s)
                after = text[end] if end < len(text) else ' '
                rec.append({
                    "seal_len": len(s), "has_hyphen": "-" in s, "has_slash": "/" in s,
                    "alpha_only": s.replace("-", "").isalpha(),
                    "word_bounded": (not before.isalnum()) and (not after.isalnum()),
                    "task_like": bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s)),
                    "dictionary_word": (s in WORDS) if WORDS is not None else None,
                    "also_a_task_name_of_admitted": any(a.rsplit("/",1)[-1].casefold()==s for a in admitted),
                })
                pos = text.find(s, pos + 1)
        out.append({"task_is_collision": bool(pat.search(t.casefold())), "instruction_chars": len(ins), "hits": rec})
print(json.dumps(out, indent=1))
