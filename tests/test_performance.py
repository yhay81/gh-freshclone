from __future__ import annotations

import sys
from pathlib import Path

from benchmarks.cached_workflow import measure_cached_workflow
from benchmarks.cli_startup import measure_startup
from gh_freshclone import runner_policy


def test_cached_workflow_contract_avoids_clone_and_runner(
    monkeypatch,
    git_repository: Path,
) -> None:
    monkeypatch.setattr(runner_policy.shutil, "which", lambda name: name)

    result = measure_cached_workflow(
        str(git_repository),
        runner="docker",
        iterations=3,
    )

    assert result.iterations == 3
    assert result.minimum_ms >= 0
    assert result.p95_ms >= result.median_ms


def test_cli_startup_benchmark_records_subprocess_latency() -> None:
    result = measure_startup(
        [sys.executable, "-c", "pass"],
        iterations=2,
    )

    assert result.iterations == 2
    assert result.minimum_ms > 0
    assert result.minimum_ms <= result.median_ms <= result.maximum_ms
