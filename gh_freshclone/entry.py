from __future__ import annotations

import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    selected = sys.argv[1:] if argv is None else argv
    if selected == ["--version"]:
        print(__version__)
        return 0

    from .cli import main as cli_main

    return cli_main(selected)
