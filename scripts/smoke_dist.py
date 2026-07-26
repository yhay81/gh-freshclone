from __future__ import annotations

import importlib.metadata
import subprocess
import sys

from gh_freshclone.api import receipt_schema
from gh_freshclone.github import GitHubTarget, parse_github_target
from gh_freshclone.github_status import GITHUB_STATUS_VERSION
from gh_freshclone.model import (
    EXECUTION_POLICY_VERSION,
    PLAN_VERSION,
    ResourceLimits,
)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_dist.py VERSION")
    expected_version = sys.argv[1]
    installed_version = importlib.metadata.version("gh-freshclone")
    if installed_version != expected_version:
        raise SystemExit(
            f"installed version {installed_version!r} != {expected_version!r}"
        )

    schema = receipt_schema()
    if schema["properties"]["receipt_version"]["const"] != 6:
        raise SystemExit("receipt v6 schema is missing from the distribution")
    if PLAN_VERSION != 8:
        raise SystemExit("unexpected plan version")
    if EXECUTION_POLICY_VERSION != 18:
        raise SystemExit("unexpected execution policy version")
    if GITHUB_STATUS_VERSION != 1:
        raise SystemExit("unexpected GitHub status interface version")
    if ResourceLimits(cpus=4, memory="8G").to_dict() != {
        "cpus": 4.0,
        "memory": "8g",
    }:
        raise SystemExit("resource limits are not canonical")
    parsed = parse_github_target("https://github.com/owner/repo/pull/42")
    if parsed != GitHubTarget("owner/repo", "refs/pull/42/head"):
        raise SystemExit("pull-request URL parser is not functional")

    completed = subprocess.run(
        ["gh-freshclone", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or completed.stdout.strip() != expected_version:
        raise SystemExit("installed console entry point is not functional")
    print(f"gh-freshclone {installed_version} distribution smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
