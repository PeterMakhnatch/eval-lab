from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> None:
    from evallab.cli import main as full_main

    full_main(argv)
