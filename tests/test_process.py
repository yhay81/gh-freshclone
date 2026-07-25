from __future__ import annotations

import subprocess

import pytest

from gh_freshclone.process import CommandError, run


def test_runner_metadata_timeout_is_a_bounded_command_result(
    monkeypatch,
) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output="partial",
            stderr="runner still starting",
        )

    monkeypatch.setattr("gh_freshclone.process.subprocess.run", timeout)

    completed = run(["docker", "system", "df"], check=False, timeout=15)

    assert completed.returncode == 124
    assert completed.stdout == "partial"
    assert "runner still starting" in completed.stderr
    assert "timed out after 15 seconds" in completed.stderr

    with pytest.raises(CommandError, match="timed out after 15 seconds"):
        run(["docker", "system", "df"], timeout=15)
