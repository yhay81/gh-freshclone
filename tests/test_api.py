from __future__ import annotations

from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from gh_freshclone import api, workflow
from gh_freshclone.api import API_VERSION, ProbeOutcome, receipt_schema
from gh_freshclone.model import (
    EXECUTION_POLICY_VERSION,
    PLAN_VERSION,
    BaselinePlan,
    CheckStep,
    Receipt,
    Repository,
    ResourceLimits,
    StepResult,
)


def test_probe_outcome_is_versioned_and_keeps_receipt_shape() -> None:
    outcome = ProbeOutcome(
        receipt={"status": "pass", "plan": {"repository": {}}},
        receipt_path=Path("receipt.json"),
        cached=True,
        elapsed_seconds=0.125,
    )

    assert outcome.passed is True
    assert outcome.to_dict()["api_version"] == API_VERSION
    assert outcome.to_dict()["receipt_path"] == "receipt.json"
    assert outcome.to_dict()["cached"] is True
    assert outcome.to_dict()["elapsed_seconds"] == 0.125


def test_probe_repository_records_end_to_end_elapsed_time(
    monkeypatch,
    tmp_path: Path,
) -> None:
    samples = iter((10.0, 11.2349))
    monkeypatch.setattr(api.time, "perf_counter", lambda: next(samples))
    monkeypatch.setattr(
        workflow,
        "check_repository",
        lambda *args, **kwargs: (
            {"status": "pass", "plan": {"repository": {}}},
            tmp_path / "receipt.json",
            False,
        ),
    )

    outcome = api.probe_repository("owner/repo", echo=False)

    assert outcome.elapsed_seconds == 1.235
    assert outcome.cached is False


def test_plan_repository_forwards_component_scope(monkeypatch) -> None:
    observed: list[str] = []

    def fake_create_plan(*args, **kwargs):
        observed.append(kwargs["component"])
        return BaselinePlan(
            repository=Repository(
                display_name="owner/repo",
                commit_sha="a" * 40,
                ref="main",
                source_url=None,
                github_repository="owner/repo",
                local_path=None,
            ),
            steps=(),
            component=kwargs["component"],
        )

    monkeypatch.setattr(workflow, "create_plan", fake_create_plan)

    plan = api.plan_repository("owner/repo", component="apps/web")

    assert plan.component == "apps/web"
    assert observed == ["apps/web"]


def test_bundled_receipt_schema_matches_current_version() -> None:
    schema = receipt_schema()

    assert schema["properties"]["receipt_version"]["const"] == 6
    assert schema["properties"]["execution_policy_version"]["type"] == "integer"
    assert schema["$defs"]["plan"]["properties"]["plan_version"]["type"] == "integer"
    assert EXECUTION_POLICY_VERSION == 21
    assert PLAN_VERSION == 10


def test_current_receipt_serialization_validates_against_bundled_schema() -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
    )
    step = CheckStep(
        ecosystem="python",
        image="python:3.13",
        prepare_command="uv sync",
        command="uv run --offline --no-sync pytest",
        evidence=("pyproject.toml",),
        dependency_fingerprint="b" * 64,
        test_network="none",
    )
    receipt = Receipt(
        created_at="2026-07-25T00:00:00+00:00",
        status="pass",
        runner="docker",
        runner_version="Docker 29",
        host_platform="test",
        plan=BaselinePlan(repository=repository, steps=(step,)),
        resource_limits=ResourceLimits(cpus=2, memory="4g"),
        results=(
            StepResult(
                ecosystem="python",
                image="python:3.13",
                image_identity="python@sha256:" + "c" * 64,
                command=step.command,
                status="pass",
                exit_code=0,
                duration_seconds=1,
                log_path="run.log",
                dependency_cache="cache-key",
                prepare_duration_seconds=0.5,
                test_network="none",
                prepared_volume="ghfc-test",
                prepare_cache_hit=True,
            ),
        ),
    )
    schema = receipt_schema()

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(receipt.to_dict())
