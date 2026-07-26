from __future__ import annotations

from pathlib import Path

from gh_freshclone.api import ProbeOutcome
from gh_freshclone.integrations.tsumugi import (
    outcome_to_tsumugi_flags,
    plan_to_tsumugi_flags,
)
from gh_freshclone.model import BaselinePlan, CheckStep, Repository


def test_environment_gap_maps_to_tsumugi_toolchain_failure() -> None:
    outcome = ProbeOutcome(
        receipt={
            "receipt_version": 5,
            "execution_policy_version": 10,
            "status": "environment_gap",
            "resource_limits": {"cpus": 2, "memory": "4g"},
            "plan": {
                "plan_version": 5,
                "profile": "quick",
                "repository": {"commit_sha": "a" * 40},
                "steps": [{"command": "tox run -e py3.13"}],
            },
            "results": [
                {
                    "ecosystem": "python",
                    "image_identity": "python@sha256:" + "b" * 64,
                    "diagnostics": [
                        {
                            "message": "Required executable is missing: less",
                        }
                    ],
                    "dependency_cache": "cache-key",
                    "detail": "",
                }
            ],
        },
        receipt_path=Path("receipt.json"),
        cached=False,
    )

    flags = outcome_to_tsumugi_flags(outcome)

    assert flags["test_cmd"] == "tox run -e py3.13"
    assert flags["test_cmd_source"] == "gh-freshclone"
    assert flags["baseline_ok"] is False
    assert flags["baseline_code"] == "toolchain_missing"
    assert flags["baseline_base_sha"] == "a" * 40
    assert flags["baseline_detail"] == "Required executable is missing: less"
    assert flags["baseline_resource_limits"] == {"cpus": 2, "memory": "4g"}
    assert flags["baseline_source_cache_hit"] is False
    assert flags["baseline_source_validation"] == "not-recorded"
    assert flags["baseline_compiler_evidence"][0]["dependency_cache"] == "cache-key"


def test_nested_step_is_rendered_for_tsumugi_host_sandbox() -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
    )
    plan = BaselinePlan(
        repository=repository,
        steps=(
            CheckStep(
                "python",
                "python:3.13",
                "pytest -q",
                prepare_command="uv sync",
                working_directory="services/api",
            ),
        ),
    )

    flags = plan_to_tsumugi_flags(plan)

    assert flags["test_cmd"] == (
        "(cd services/api && uv sync) && (cd services/api && pytest -q)"
    )
    assert (
        flags["baseline_compiler_evidence"][0]["working_directory"]
        == "services/api"
    )
