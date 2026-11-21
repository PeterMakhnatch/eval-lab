from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from muster import __version__, commands
from muster.model import InputError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="muster", description="Controls and measurement for multi-seat Harbor tasks."
    )
    root.add_argument("--version", action="version", version=f"muster {__version__}")
    subparsers = root.add_subparsers(dest="command", metavar="command", required=True)
    for name, module in commands.modules():
        description = module.DESCRIPTION
        assert description and description == description.strip()
        child = subparsers.add_parser(
            name,
            help=description.splitlines()[0],
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        module.configure(child)
        child.set_defaults(run=module.run)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.run(args)
    except InputError as error:
        print(f"muster: {' '.join(str(error).split())}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("muster: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
