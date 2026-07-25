from __future__ import annotations

# Intentional argv-only process boundary; shell execution is never used.
import subprocess  # nosec B404
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class CommandError(RuntimeError):
    def __init__(self, command: Sequence[str], returncode: int, stderr: str):
        rendered = subprocess.list2cmdline(list(command))
        detail = stderr.strip() or f"command exited with status {returncode}"
        super().__init__(f"{rendered}: {detail}")
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class Completed:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> Completed:
    # The command is an argv sequence, never a shell string.
    proc = subprocess.run(  # nosec B603
        list(command),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=env,
    )
    result = Completed(tuple(command), proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(command, proc.returncode, proc.stderr or proc.stdout)
    return result
