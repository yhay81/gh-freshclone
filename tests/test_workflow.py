from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from gh_freshclone import workflow
from gh_freshclone.model import BaselinePlan, CheckStep, Repository
from gh_freshclone.runners import RunnerError, RunnerExecution
from gh_freshclone.workflow import check_repository, create_plan
from gh_freshclone.workspace_archive import WorkspaceArchiveError


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
    assert plan.steps[0].test_network == "none"


def test_operator_network_policy_is_explicit_and_recorded() -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
    )
    requested = BaselinePlan(
        repository=repository,
        steps=(
            CheckStep(
                ecosystem="custom",
                image="docker.io/library/alpine:3",
                command="true",
                test_network="enabled",
            ),
        ),
    )

    offline = workflow._apply_test_network_policy(requested, "none")
    enabled = workflow._apply_test_network_policy(offline, "enabled")

    assert offline.steps[0].test_network == "none"
    assert "kept every test step offline" in offline.warnings[-1]
    assert enabled.steps[0].test_network == "enabled"
    assert "enabled outbound network" in enabled.warnings[-1]
    assert not any("kept every test step offline" in item for item in enabled.warnings)


def test_invalid_test_network_fails_before_repository_resolution(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "resolve_repository",
        lambda *args, **kwargs: pytest.fail("repository resolution must not start"),
    )

    with pytest.raises(ValueError, match="test_network must be one of"):
        check_repository("owner/repo", test_network="host")


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
    original_materialize = workflow.materialize_plan_inputs
    materializations: list[Path] = []

    def tracking_materialize(repository, destination, component="."):
        materializations.append(destination)
        original_materialize(repository, destination, component)

    monkeypatch.setattr(
        "gh_freshclone.workflow.materialize_plan_inputs",
        tracking_materialize,
    )
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


def test_fresh_preparation_triggers_volume_pressure_maintenance(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )
    monkeypatch.setattr(
        "gh_freshclone.workflow.run_step",
        lambda *args, **kwargs: RunnerExecution(
            0,
            0.1,
            "ok",
            prepared_volume=(
                "ghfc-12345678-123456789abc-quick-python-cache-p6-e16"
            ),
            prepare_cache_hit=False,
        ),
    )
    maintenance: list[dict[str, object]] = []
    monkeypatch.setattr(
        "gh_freshclone.cache.maybe_prune_cache",
        lambda **kwargs: maintenance.append(kwargs),
    )

    check_repository(
        str(git_repository),
        use_cache=False,
        echo=False,
    )

    assert maintenance == [{"prepared_volumes_changed": True}]


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
        "materialize_plan_inputs",
        lambda *args, **kwargs: pytest.fail("clone must not start"),
    )

    with pytest.raises(RunnerError, match="no installed OCI runner is ready"):
        check_repository(str(git_repository), use_cache=False, echo=False)


def test_check_does_not_expand_repository_without_an_executable_plan(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        workflow,
        "select_runner",
        lambda requested: "docker",
    )
    repository = workflow.resolve_repository(str(git_repository))
    monkeypatch.setattr(
        "gh_freshclone.detect.detect_plan",
        lambda *args, **kwargs: BaselinePlan(repository=repository, steps=()),
    )
    monkeypatch.setattr(
        workflow,
        "complete_materialization",
        lambda *args, **kwargs: pytest.fail(
            "unsupported repositories must not be fully expanded"
        ),
    )

    with pytest.raises(workflow.WorkflowError, match="no executable baseline"):
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


def test_check_never_reuses_pass_across_test_network_policies(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )
    calls: list[str] = []

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        calls.append(step.test_network)
        return RunnerExecution(
            0,
            0.1,
            "ok",
            runner_version="test",
            test_network=step.test_network,
        )

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    offline, offline_path, offline_cached = check_repository(
        str(git_repository),
        echo=False,
    )
    enabled, enabled_path, enabled_cached = check_repository(
        str(git_repository),
        echo=False,
        test_network="enabled",
    )
    repeated_offline, repeated_offline_path, repeated_offline_cached = (
        check_repository(str(git_repository), echo=False)
    )
    repeated_enabled, repeated_enabled_path, repeated_enabled_cached = (
        check_repository(
            str(git_repository),
            echo=False,
            test_network="enabled",
        )
    )

    assert offline.plan.steps[0].test_network == "none"
    assert enabled.plan.steps[0].test_network == "enabled"
    assert offline_cached is False
    assert enabled_cached is False
    assert repeated_offline_cached is True
    assert repeated_enabled_cached is True
    assert repeated_offline["plan"]["steps"][0]["test_network"] == "none"
    assert repeated_enabled["plan"]["steps"][0]["test_network"] == "enabled"
    assert offline_path != enabled_path
    assert repeated_offline_path == offline_path
    assert repeated_enabled_path == enabled_path
    assert calls == ["none", "enabled"]


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


def test_check_executes_only_the_selected_component_from_the_full_checkout(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    component = git_repository / "apps" / "web"
    component.mkdir(parents=True)
    (component / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}\n',
        encoding="utf-8",
    )
    (component / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n',
        encoding="utf-8",
    )
    (component / "source.js").write_text("export const value = 1;\n", encoding="utf-8")
    (component / "test").mkdir()
    (component / "test" / "source.test.js").write_text(
        "import assert from 'node:assert';\nassert.equal(1, 1);\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repository),
            "-c",
            "user.name=Freshclone Tests",
            "-c",
            "user.email=freshclone@example.invalid",
            "commit",
            "-m",
            "add component",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        assert step.ecosystem == "node"
        assert step.working_directory == "apps/web"
        assert (workspace / "apps" / "web" / "source.js").is_file()
        assert not (workspace / "pyproject.toml").exists()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return RunnerExecution(0, 0.1, "ok")

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    receipt, _, _ = check_repository(
        str(git_repository),
        component="apps/web",
        use_cache=False,
        echo=False,
    )

    assert receipt.status == "pass"
    assert receipt.plan.component == "apps/web"
    assert [step.ecosystem for step in receipt.plan.steps] == ["node"]


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
    original_materialize = workflow.materialize_plan_inputs
    canonical_checkout: list[Path] = []

    def tracking_materialize(repository, destination, component="."):
        canonical_checkout.append(destination)
        original_materialize(repository, destination, component)

    monkeypatch.setattr(
        "gh_freshclone.workflow.materialize_plan_inputs",
        tracking_materialize,
    )

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        assert workspace == canonical_checkout[0]
        assert (workspace / ".git").is_dir()
        assert (workspace / "tests" / "test_sample.py").is_file()
        archive = kwargs["workspace_archive"]
        assert archive.is_file()
        with tarfile.open(archive) as workspace_tar:
            names = workspace_tar.getnames()
        assert "tests/test_sample.py" in names
        assert not any(
            name == ".git" or name.startswith(".git/") for name in names
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return RunnerExecution(0, 0.1, "ok")

    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    receipt, _, _ = check_repository(str(git_repository), use_cache=False, echo=False)

    assert receipt.status == "pass"


def test_check_falls_back_to_readonly_bind_copy_when_archive_is_unsafe(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "gh_freshclone.workflow.select_runner",
        lambda requested: "docker",
    )

    def reject_archive(workspace, destination, commit_sha, *, component="."):
        raise WorkspaceArchiveError("non-portable path")

    def fake_run_step(runner, step, workspace, log_path, **kwargs):
        assert kwargs["workspace_archive"] is None
        assert (workspace / ".git").is_dir()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n", encoding="utf-8")
        return RunnerExecution(0, 0.1, "ok")

    monkeypatch.setattr(
        "gh_freshclone.workspace_archive.create_workspace_archive",
        reject_archive,
    )
    monkeypatch.setattr("gh_freshclone.workflow.run_step", fake_run_step)

    receipt, _, _ = check_repository(str(git_repository), use_cache=False, echo=True)

    assert receipt.status == "pass"
    assert "workspace archive unavailable; using bind copy" in capsys.readouterr().out
