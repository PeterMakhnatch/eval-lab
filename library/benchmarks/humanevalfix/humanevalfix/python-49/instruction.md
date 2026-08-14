Please fix the function in Python__49.py such that all test cases pass.
Environment has been set up for you to start working. You may assume all necessary tools are installed.

# Problem Statement
def modp(n: int, p: int):
    ret = 0
    for i in range(n):
        ret = (2 * ret) % p
    return ret






def check(modp):
    assert modp(3, 5) == 3
    assert modp(1101, 101) == 2
    assert modp(0, 101) == 1
    assert modp(3, 11) == 8
    assert modp(100, 101) == 1
    assert modp(30, 5) == 4
    assert modp(31, 5) == 3

check(modp)

IMPORTANT: You should ONLY interact with the environment provided to you AND NEVER ASK FOR HUMAN HELP.
You should NOT modify any existing test case files. If needed, you can add new test cases in a NEW file to reproduce the issue.
You SHOULD INCLUDE PROPER INDENTATION in your edit commands.
When you think you have fixed the issue through code changes, please finish the interaction using the "finish" tool.

