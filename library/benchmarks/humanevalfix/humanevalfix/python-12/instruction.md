Please fix the function in Python__12.py such that all test cases pass.
Environment has been set up for you to start working. You may assume all necessary tools are installed.

# Problem Statement
from typing import List, Optional


def longest(strings: List[str]) -> Optional[str]:
    if not strings:
        return None

    maxlen = max(len(x) for x in strings)
    for s in strings:
        if len(s) > maxlen:
            return s






def check(longest):
    assert longest([]) == None
    assert longest(['x', 'y', 'z']) == 'x'
    assert longest(['x', 'yyy', 'zzzz', 'www', 'kkkk', 'abc']) == 'zzzz'

check(longest)

IMPORTANT: You should ONLY interact with the environment provided to you AND NEVER ASK FOR HUMAN HELP.
You should NOT modify any existing test case files. If needed, you can add new test cases in a NEW file to reproduce the issue.
You SHOULD INCLUDE PROPER INDENTATION in your edit commands.
When you think you have fixed the issue through code changes, please finish the interaction using the "finish" tool.

