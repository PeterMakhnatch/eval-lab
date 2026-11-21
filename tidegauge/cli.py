from __future__ import annotations

import argparse
import sys

from tidegauge import __version__
from tidegauge.commands import COMMANDS

DESCRIPTION = """\
Honest numbers for Harbor runs.

tidegauge reads Harbor job directories and answers 4 questions without a model:
are these trials valid and comparable, what is the score and its error bar,
is the difference between 2 runs real, and how many trials would settle it.
"""


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="tidegauge", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    top.add_argument("--version", action="version", version=f"tidegauge {__version__}")
    sub = top.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, module in COMMANDS.items():
        child = sub.add_parser(
            name,
            help=module.HELP,
            description=module.DESCRIPTION,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        module.configure(child)
        child.set_defaults(run=module.run)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except (FileNotFoundError, ValueError) as error:
        print(f"tidegauge: {error}", file=sys.stderr)
        return 3
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
