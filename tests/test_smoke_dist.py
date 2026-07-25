from __future__ import annotations

import subprocess
import sys

from scripts import smoke_dist


def test_distribution_smoke_tracks_current_public_policy(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["smoke_dist.py", "0.6.0"])
    monkeypatch.setattr(smoke_dist.importlib.metadata, "version", lambda name: "0.6.0")
    monkeypatch.setattr(
        smoke_dist.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="0.6.0\n",
            stderr="",
        ),
    )

    assert smoke_dist.main() == 0
