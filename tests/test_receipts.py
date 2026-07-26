from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from gh_freshclone.model import (
    BaselinePlan,
    CheckStep,
    Receipt,
    Repository,
    ResourceLimits,
)
from gh_freshclone.receipts import (
    _index_path,
    cache_root,
    dependency_cache_path,
    execution_cache_key,
    execution_context_digest,
    plan_digest,
    prepared_volume_name,
    read_indexed_pass_receipt,
    read_pass_receipt,
    receipt_path,
    write_receipt,
)

_LIMITS = ResourceLimits()


def _plan() -> BaselinePlan:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
    )
    return BaselinePlan(
        repository,
        (CheckStep("go", "golang:1-bookworm", "go test ./...", ("go.mod",)),),
    )


def test_cache_override_and_deterministic_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()

    assert cache_root() == tmp_path
    assert plan_digest(plan) == plan_digest(plan)
    parent = receipt_path(plan, "docker", _LIMITS).parent
    assert parent.parent == tmp_path / "receipts"
    assert parent.name.startswith("owner_repo-")


def test_pass_index_is_available_without_rebuilding_plan(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()
    path = receipt_path(plan, "docker", _LIMITS)
    receipt = Receipt(
        created_at="2026-07-25T00:00:00+00:00",
        status="pass",
        runner="docker",
        runner_version="Docker 29",
        host_platform="test",
        plan=plan,
        resource_limits=_LIMITS,
    )

    write_receipt(path, receipt)
    indexed = read_indexed_pass_receipt(
        plan.repository,
        "quick",
        "docker",
        _LIMITS,
    )

    assert indexed is not None
    value, indexed_path = indexed
    assert value["status"] == "pass"
    assert indexed_path == path
    assert (
        read_indexed_pass_receipt(plan.repository, "full", "docker", _LIMITS)
        is None
    )


def test_pass_index_hit_refreshes_evidence_lru(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()
    path = receipt_path(plan, "docker", _LIMITS)
    receipt = Receipt(
        created_at="2026-07-25T00:00:00+00:00",
        status="pass",
        runner="docker",
        runner_version="Docker 29",
        host_platform="test",
        plan=plan,
        resource_limits=_LIMITS,
    )
    write_receipt(path, receipt)
    old = 1_700_000_000
    os.utime(path, (old, old))

    assert (
        read_indexed_pass_receipt(
            plan.repository,
            plan.profile,
            "docker",
            _LIMITS,
        )
        is not None
    )
    assert path.stat().st_mtime > old


def test_frequent_pass_hits_coalesce_lru_writes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()
    path = receipt_path(plan, "docker", _LIMITS)
    receipt = Receipt(
        created_at="2026-07-25T00:00:00+00:00",
        status="pass",
        runner="docker",
        runner_version="Docker 29",
        host_platform="test",
        plan=plan,
        resource_limits=_LIMITS,
    )
    write_receipt(path, receipt)
    recent = 1_700_000_000
    os.utime(path, (recent, recent))
    monkeypatch.setattr(
        "gh_freshclone.receipts.time.time",
        lambda: recent + 60,
    )

    assert (
        read_indexed_pass_receipt(
            plan.repository,
            plan.profile,
            "docker",
            _LIMITS,
        )
        is not None
    )
    assert path.stat().st_mtime == recent


def test_resource_limits_have_distinct_receipts_and_pass_indexes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()
    small = ResourceLimits(cpus=2, memory="4G")
    large = ResourceLimits(cpus=4, memory="8g")
    small_path = receipt_path(plan, "docker", small)
    large_path = receipt_path(plan, "docker", large)
    receipt = Receipt(
        created_at="2026-07-25T00:00:00+00:00",
        status="pass",
        runner="docker",
        runner_version="Docker 29",
        host_platform="test",
        plan=plan,
        resource_limits=small,
    )

    write_receipt(small_path, receipt)

    assert small.memory == "4g"
    assert small_path != large_path
    assert (
        read_indexed_pass_receipt(plan.repository, "quick", "docker", small)
        is not None
    )
    assert (
        read_indexed_pass_receipt(plan.repository, "quick", "docker", large)
        is None
    )


def test_test_network_policies_have_distinct_pass_indexes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    offline = _plan()
    enabled = replace(
        offline,
        steps=(replace(offline.steps[0], test_network="enabled"),),
    )
    offline_path = receipt_path(offline, "docker", _LIMITS)
    enabled_path = receipt_path(enabled, "docker", _LIMITS)
    for plan, path in ((offline, offline_path), (enabled, enabled_path)):
        write_receipt(
            path,
            Receipt(
                created_at="2026-07-25T00:00:00+00:00",
                status="pass",
                runner="docker",
                runner_version="Docker 29",
                host_platform="test",
                plan=plan,
                resource_limits=_LIMITS,
            ),
        )

    offline_index = read_indexed_pass_receipt(
        offline.repository,
        "quick",
        "docker",
        _LIMITS,
        "none",
    )
    enabled_index = read_indexed_pass_receipt(
        enabled.repository,
        "quick",
        "docker",
        _LIMITS,
        "enabled",
    )

    assert offline_index is not None
    assert enabled_index is not None
    assert offline_index[1] == offline_path
    assert enabled_index[1] == enabled_path
    assert offline_index[0]["plan"]["steps"][0]["test_network"] == "none"
    assert enabled_index[0]["plan"]["steps"][0]["test_network"] == "enabled"


def test_safe_legacy_pass_index_remains_reusable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()
    path = receipt_path(plan, "docker", _LIMITS)
    write_receipt(
        path,
        Receipt(
            created_at="2026-07-25T00:00:00+00:00",
            status="pass",
            runner="docker",
            runner_version="Docker 29",
            host_platform="test",
            plan=plan,
            resource_limits=_LIMITS,
        ),
    )
    current_index = _index_path(
        plan.repository,
        plan.profile,
        "docker",
        _LIMITS,
        "none",
    )
    legacy_index = _index_path(
        plan.repository,
        plan.profile,
        "docker",
        _LIMITS,
        None,
    )
    current_index.replace(legacy_index)

    indexed = read_indexed_pass_receipt(
        plan.repository,
        plan.profile,
        "docker",
        _LIMITS,
        "none",
    )

    assert indexed is not None
    assert indexed[1] == path


def test_enabled_legacy_pass_index_is_never_reused_offline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    base = _plan()
    plan = replace(
        base,
        steps=(replace(base.steps[0], test_network="enabled"),),
    )
    path = receipt_path(plan, "docker", _LIMITS)
    write_receipt(
        path,
        Receipt(
            created_at="2026-07-25T00:00:00+00:00",
            status="pass",
            runner="docker",
            runner_version="Docker 29",
            host_platform="test",
            plan=plan,
            resource_limits=_LIMITS,
        ),
    )
    current_index = _index_path(
        plan.repository,
        plan.profile,
        "docker",
        _LIMITS,
        "enabled",
    )
    legacy_index = _index_path(
        plan.repository,
        plan.profile,
        "docker",
        _LIMITS,
        None,
    )
    current_index.replace(legacy_index)

    assert (
        read_indexed_pass_receipt(
            plan.repository,
            plan.profile,
            "docker",
            _LIMITS,
            "none",
        )
        is None
    )


def test_execution_context_canonicalizes_equivalent_cpu_values() -> None:
    integer = execution_context_digest("docker", ResourceLimits(cpus=4))
    floating = execution_context_digest("docker", ResourceLimits(cpus=4.0))

    assert integer == floating


def test_dependency_cache_is_isolated_by_repository_and_fingerprint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    first = _plan()
    first_step = CheckStep(
        "go",
        "golang:1-bookworm",
        "go test ./...",
        ("go.mod",),
        "a" * 64,
    )
    second_step = CheckStep(
        "go",
        "golang:1-bookworm",
        "go test ./...",
        ("go.mod",),
        "b" * 64,
    )

    first_path = dependency_cache_path(first.repository, first_step, "docker")
    second_path = dependency_cache_path(first.repository, second_step, "docker")

    assert first_path != second_path
    assert first_path.is_relative_to(tmp_path / "runner-cache" / "docker")


def test_sanitized_repository_name_collisions_never_share_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    step = _plan().steps[0]
    first_repository = Repository(
        display_name="a/b_c",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/a/b_c",
        github_repository="a/b_c",
        local_path=None,
    )
    second_repository = Repository(
        display_name="a_b/c",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/a_b/c",
        github_repository="a_b/c",
        local_path=None,
    )
    first_plan = BaselinePlan(first_repository, (step,))
    second_plan = BaselinePlan(second_repository, (step,))

    assert (
        dependency_cache_path(first_repository, step, "docker")
        != dependency_cache_path(second_repository, step, "docker")
    )
    assert (
        receipt_path(first_plan, "docker", _LIMITS).parent
        != receipt_path(second_plan, "docker", _LIMITS).parent
    )
    assert (
        prepared_volume_name(first_repository, step, "quick")
        != prepared_volume_name(second_repository, step, "quick")
    )


def test_same_named_local_repositories_use_path_scoped_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    step = _plan().steps[0]
    first = Repository(
        display_name="repository",
        commit_sha="a" * 40,
        ref="HEAD",
        source_url=None,
        github_repository=None,
        local_path=str(tmp_path / "first" / "repository"),
    )
    second = Repository(
        display_name="repository",
        commit_sha="a" * 40,
        ref="HEAD",
        source_url=None,
        github_repository=None,
        local_path=str(tmp_path / "second" / "repository"),
    )

    assert (
        dependency_cache_path(first, step, "docker")
        != dependency_cache_path(second, step, "docker")
    )
    assert prepared_volume_name(first, step, "quick") != prepared_volume_name(
        second,
        step,
        "quick",
    )


def test_prepared_volume_is_scoped_to_public_policy_versions() -> None:
    plan = _plan()
    name = prepared_volume_name(plan.repository, plan.steps[0], plan.profile)

    assert name.startswith("ghfc-")
    assert "-p9-e21" in name


def test_prepared_volume_is_scoped_to_step_working_directory() -> None:
    plan = _plan()
    root = prepared_volume_name(plan.repository, plan.steps[0], plan.profile)
    nested = prepared_volume_name(
        plan.repository,
        replace(plan.steps[0], working_directory="services/api"),
        plan.profile,
    )

    assert root != nested


def test_prepared_volume_is_namespaced_by_cache_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    plan = _plan()
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "first"))
    first = prepared_volume_name(plan.repository, plan.steps[0], plan.profile)
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "second"))
    second = prepared_volume_name(plan.repository, plan.steps[0], plan.profile)

    assert first != second


def test_execution_cache_changes_with_image_content() -> None:
    step = _plan().steps[0]

    first = execution_cache_key(step, "golang@sha256:" + "a" * 64)
    second = execution_cache_key(step, "golang@sha256:" + "b" * 64)

    assert first != second


def test_test_only_changes_reuse_prepared_dependencies() -> None:
    plan = _plan()
    step = plan.steps[0]
    changed_test = replace(
        step,
        command="go test -count=1 ./...",
        evidence=(*step.evidence, "profile.full"),
        test_network="enabled",
    )
    image = "golang@sha256:" + "a" * 64

    assert execution_cache_key(step, image) == execution_cache_key(
        changed_test,
        image,
    )
    assert prepared_volume_name(
        plan.repository,
        step,
        plan.profile,
    ) == prepared_volume_name(
        plan.repository,
        changed_test,
        plan.profile,
    )


def test_prepare_command_change_invalidates_prepared_dependencies() -> None:
    plan = _plan()
    step = plan.steps[0]
    changed_prepare = replace(step, prepare_command="go mod download -x")
    image = "golang@sha256:" + "a" * 64

    assert execution_cache_key(step, image) != execution_cache_key(
        changed_prepare,
        image,
    )
    assert prepared_volume_name(
        plan.repository,
        step,
        plan.profile,
    ) != prepared_volume_name(
        plan.repository,
        changed_prepare,
        plan.profile,
    )


def test_stale_pass_receipt_is_never_reused(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    plan = _plan()
    path = receipt_path(plan, "docker", _LIMITS)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "status": "pass",
                "receipt_version": 3,
                "execution_policy_version": 1,
            }
        ),
        encoding="utf-8",
    )

    assert (
        read_pass_receipt(
            path,
            plan=plan,
            runner="docker",
            resource_limits=_LIMITS,
        )
        is None
    )
