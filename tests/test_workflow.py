from __future__ import annotations

from pathlib import Path

import pytest

from gh_freshclone import workflow
from gh_freshclone.runners import RunnerError, RunnerExecution
from gh_freshclone.workflow import check_repository, create_plan


@pytest.fixture(autouse=True)
def _preferred_runner(monkeypatch) -> None:
    monkeypatch.setattr(workflow, "preferred_runner", lambda runner: "docker")
    monkeypatch.setattr(
        "gh_freshclone.cache.ensure_storage_reserve",
        lambda: None,
    )


def test_create_plan_from_local_repository(git_repository: Path) -> None:
    plan = create_plan(str(git_repository))

    assert plan.repository.commit_sha
    assert [step.ecosystem for step in plan.steps] == ["python"]


def test_invalid_resource_limits_fail_before_repository_resolution(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "resolve_repository",
        lambda *args, **kwargs: pytest.fail("repository resolution must not start"),
    )

    with pytest.raises(ValueError, match="finite number greater than zero"):
        check_repository("owner/repo", cpus=float("nan"))


def test_check_writes_and_reuses_passing_receipt(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )
    original_materialize = workflow.materialize
    materializations: list[Path] = []

    def tracking_materialize(repository, destination):
        materializations.append(destination)
        original_materialize(repository, destination)

    monkeypatch.setattr("gh_freshclone.workflow.materialize", tracking_materialize)
    calls: list[Path] = []

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        calls.append(workspace)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return RunnerExecution(0, 0.1, "ok")

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    first, path, cached = check_repository(str(git_repository), echo=False)
    monkeypatch.setattr(
        workflow,
        "select_runner",
        lambda runner: pytest.fail("a PASS hit must not probe runner readiness"),
    )
    monkeypatch.setattr(
        "gh_freshclone.cache.ensure_storage_reserve",
        lambda: pytest.fail("a PASS hit must not require fresh storage"),
    )
    second, second_path, second_cached = check_repository(
        str(git_repository),
        echo=False,
    )

    assert first.status == "pass"
    assert path.is_file()
    assert not cached
    assert second["status"] == "pass"
    assert second_path == path
    assert second_cached
    assert len(calls) == 1
    assert len(materializations) == 1


def test_cache_miss_rejects_stopped_runner_before_clone(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))

    def stopped_runner(requested: str) -> str:
        raise RunnerError("no installed OCI runner is ready")

    monkeypatch.setattr(workflow, "select_runner", stopped_runner)
    monkeypatch.setattr(
        workflow,
        "materialize",
        lambda *args, **kwargs: pytest.fail("clone must not start"),
    )

    with pytest.raises(RunnerError, match="no installed OCI runner is ready"):
        check_repository(str(git_repository), use_cache=False, echo=False)


def test_check_never_reuses_pass_across_resource_limits(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )
    calls: list[tuple[float, str]] = []

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        calls.append((kwargs["cpus"], kwargs["memory"]))
        return RunnerExecution(0, 0.1, "ok", runner_version="test")

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    small, small_path, small_cached = check_repository(
        str(git_repository),
        cpus=2,
        memory="4G",
        echo=False,
    )
    repeated, repeated_path, repeated_cached = check_repository(
        str(git_repository),
        cpus=2,
        memory="4g",
        echo=False,
    )
    large, large_path, large_cached = check_repository(
        str(git_repository),
        cpus=4,
        memory="8g",
        echo=False,
    )

    assert small.resource_limits.to_dict() == {"cpus": 2, "memory": "4g"}
    assert repeated["resource_limits"] == {"cpus": 2, "memory": "4g"}
    assert large.resource_limits.to_dict() == {"cpus": 4, "memory": "8g"}
    assert small_cached is False
    assert repeated_cached is True
    assert large_cached is False
    assert repeated_path == small_path
    assert large_path != small_path
    assert calls == [(2, "4g"), (4, "8g")]


def test_check_mounts_only_committed_files(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    (git_repository / ".env").write_text("SECRET=not-mounted\n", encoding="utf-8")
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        assert not (workspace / ".env").exists()
        assert (workspace / ".git").is_dir()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return RunnerExecution(0, 0.1, "ok")

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    receipt, _, _ = check_repository(str(git_repository), use_cache=False, echo=False)

    assert receipt.status == "pass"


def test_docker_reuses_readonly_checkout_without_host_copy(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )
    original_materialize = workflow.materialize
    canonical_checkout: list[Path] = []

    def tracking_materialize(repository, destination):
        canonical_checkout.append(destination)
        original_materialize(repository, destination)

    monkeypatch.setattr("gh_freshclone.workflow.materialize", tracking_materialize)

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        assert workspace == canonical_checkout[0]
        assert (workspace / ".git").is_dir()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return RunnerExecution(0, 0.1, "ok")

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    receipt, _, _ = check_repository(str(git_repository), use_cache=False, echo=False)

    assert receipt.status == "pass"
