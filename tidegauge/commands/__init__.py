"""One module per command. Each module has ``configure(parser)`` and ``run(args) -> int``.

To add a command: write the module, then add it to ``COMMANDS``.
"""

from tidegauge.commands import audit, compare, context, decompose, gate, plan, rank, score

COMMANDS = {
    "audit": audit,
    "score": score,
    "compare": compare,
    "rank": rank,
    "plan": plan,
    "gate": gate,
    "context": context,
    "decompose": decompose,
}
