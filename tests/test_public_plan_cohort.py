from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from benchmarks.public_plan_cohort import DEFAULT_COHORT, evaluate_cohort, load_cohort
from gh_freshclone.model import BaselinePlan, CheckStep, Repository

_SHA = "1" * 40
_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "public-plan-cohort.yml"


def _write_cohort(path: Path, cases: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps({"schema_version": 1, "cases": cases}),
        encoding="utf-8",
    )
    return path


def _case(case_id: str, target: str, expected: list[str]) -> dict[str, object]:
    return {
        "id": case_id,
        "target": target,
        "ref": _SHA,
        "expected_ecosystems": expected,
    }


def _plan(target: str, ecosystems: tuple[str, ...]) -> BaselinePlan:
    repository = Repository(
        display_name=target,
        commit_sha=_SHA,
        ref=_SHA,
        source_url=f"https://github.com/{target}",
        github_repository=target,
        local_path=None,
        is_private=False,
    )
    return BaselinePlan(
        repository=repository,
        steps=tuple(
            CheckStep(ecosystem=ecosystem, image="example:latest", command="test")
            for ecosystem in ecosystems
        ),
    )


def test_public_plan_cohort_records_matches_and_detection_rate(tmp_path: Path) -> None:
    cohort = _write_cohort(
        tmp_path / "cohort.json",
        [
            _case("supported", "owner/supported", ["python"]),
            _case("control", "owner/control", []),
        ],
    )

    def planner(target: str, **_: object) -> BaselinePlan:
        return _plan(target, ("python",) if target.endswith("supported") else ())

    result = evaluate_cohort(cohort, jobs=1, planner=planner)

    assert result["total"] == 2
    assert result["matched"] == 2
    assert result["regressions"] == 0
    assert result["expected_executable_plans"] == 1
    assert result["executable_plans"] == 1
    assert result["detection_rate"] == 0.5
    assert result["outcomes"] == {
        "match": 2,
        "missed-plan": 0,
        "unexpected-plan": 0,
        "ecosystem-mismatch": 0,
        "error": 0,
    }


def test_checked_in_public_cohort_covers_supported_and_fail_closed_cases() -> None:
    cases = load_cohort(DEFAULT_COHORT)
    ecosystems = {
        ecosystem for case in cases for ecosystem in case.expected_ecosystems
    }

    assert len(cases) == 20
    assert sum(not case.expected_ecosystems for case in cases) == 4
    assert {
        "bun",
        "cmake",
        "deno",
        "dotnet",
        "go",
        "gradle",
        "make",
        "maven",
        "node",
        "php",
        "python",
        "ruby",
        "rust",
    } <= ecosystems


def test_public_cohort_workflow_is_scheduled_nonblocking_and_pinned() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    assert "  workflow_dispatch:\n" in workflow
    assert "  schedule:\n" in workflow
    assert "\n  pull_request:" not in workflow
    assert "\n  push:" not in workflow
    assert "permissions:\n  contents: read\n" in workflow
    assert "          persist-credentials: false\n" in workflow
    assert "uv run python -m benchmarks.public_plan_cohort" in workflow
    assert "        if: always()\n" in workflow
    assert (
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1"
        in workflow
    )


def test_public_plan_cohort_distinguishes_regression_kinds(tmp_path: Path) -> None:
    cohort = _write_cohort(
        tmp_path / "cohort.json",
        [
            _case("missed", "owner/missed", ["python"]),
            _case("unexpected", "owner/unexpected", []),
            _case("mismatch", "owner/mismatch", ["go"]),
            _case("error", "owner/error", ["rust"]),
        ],
    )

    def planner(target: str, **_: object) -> BaselinePlan:
        if target.endswith("error"):
            raise RuntimeError("network unavailable")
        ecosystems = {
            "owner/missed": (),
            "owner/unexpected": ("node",),
            "owner/mismatch": ("rust",),
        }
        return _plan(target, ecosystems[target])

    result = evaluate_cohort(cohort, jobs=1, planner=planner)

    assert result["matched"] == 0
    assert result["regressions"] == 4
    assert result["outcomes"] == {
        "match": 0,
        "missed-plan": 1,
        "unexpected-plan": 1,
        "ecosystem-mismatch": 1,
        "error": 1,
    }
    case_results = cast(list[dict[str, object]], result["cases"])
    cases = {str(case["id"]): case for case in case_results}
    assert cases["error"]["error"] == "RuntimeError: network unavailable"


@pytest.mark.parametrize(
    "cases, message",
    [
        ([_case("duplicate", "owner/one", []), _case("duplicate", "owner/two", [])], "ids"),
        ([_case("bad-ref", "owner/repo", []) | {"ref": "main"}], "commit SHA"),
        ([_case("bad-target", "https://github.com/owner/repo", [])], "OWNER/REPO"),
    ],
)
def test_public_plan_cohort_rejects_ambiguous_inputs(
    tmp_path: Path,
    cases: list[dict[str, object]],
    message: str,
) -> None:
    cohort = _write_cohort(tmp_path / "cohort.json", cases)

    with pytest.raises(ValueError, match=message):
        load_cohort(cohort)
