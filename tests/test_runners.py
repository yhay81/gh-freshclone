from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gh_freshclone import runner_policy, runners
from gh_freshclone.diagnostics import failure_executable_candidates
from gh_freshclone.model import CheckStep
from gh_freshclone.process import Completed
from gh_freshclone.runner_policy import (
    preferred_runner,
    runner_ready,
    runner_supported,
    select_runner,
)
from gh_freshclone.runners import (
    _console_safe,
    build_runner_command,
    classify_exit,
    run_step,
)


def _step() -> CheckStep:
    return CheckStep("go", "golang:1.24-bookworm", "go test ./...", ("go.mod",))


def test_docker_command_is_limited_and_passes_no_host_environment(
    tmp_path: Path,
) -> None:
    command = build_runner_command(
        "docker",
        _step(),
        tmp_path,
        cpus=2,
        memory="4g",
        container_name="gh-freshclone-test",
        cache_dir=tmp_path / "cache",
        image_identity="golang@sha256:" + "a" * 64,
    )
    rendered = " ".join(command)

    assert "--cap-drop=ALL" in command
    assert "--cap-add=CHOWN" in command
    assert "--cap-add=FOWNER" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--cpus=2" in command
    assert "--memory=4g" in command
    assert "--name=gh-freshclone-test" in command
    assert "--tmpfs=/tmp:rw,exec,nosuid,nodev,size=2g" in command
    assert "GH_TOKEN" not in rendered
    assert "GITHUB_TOKEN" not in rendered
    assert "target=/input,readonly" in rendered
    assert "target=/cache" in rendered
    assert "GOMODCACHE=/cache/go-mod" in rendered
    assert command[-3] == "golang@sha256:" + "a" * 64
    expected_shell = (
        'mkdir -p "$HOME" /workspace /cache && cp -R /input/. /workspace/ '
        "&& cd /workspace && go test ./..."
    )
    assert command[-2:] == ["-c", expected_shell]
    assert "cp -a" not in rendered


def test_apple_container_command_uses_volume_and_resource_limits(
    tmp_path: Path,
) -> None:
    command = build_runner_command(
        "container",
        _step(),
        tmp_path,
        cpus=4,
        memory="8g",
        container_name="gh-freshclone-test",
    )

    assert command[:2] == ["container", "run"]
    assert "--mount" in command
    assert "--cpus" in command
    assert "--memory" in command
    assert "--entrypoint" in command
    assert command[command.index("--name") + 1] == "gh-freshclone-test"
    assert (
        f"type=bind,source={tmp_path.resolve()},target=/input,readonly"
        in command
    )
    assert "--cap-drop" in command
    cap_adds = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--cap-add"
    ]
    assert cap_adds == ["CHOWN", "FOWNER"]


def test_apple_container_requires_whole_cpu_limit(tmp_path: Path) -> None:
    with pytest.raises(
        runners.RunnerError,
        match="whole-number CPU limit",
    ):
        build_runner_command(
            "container",
            _step(),
            tmp_path,
            cpus=2.5,
            memory="8g",
        )


def test_apple_container_version_support_is_explicit() -> None:
    assert runner_supported("container", "container CLI version 1.0.0") is True
    assert runner_supported("container", "container 1.1.0 (build 42)") is True
    assert runner_supported("container", "container 0.12.3") is False
    assert runner_supported("container", "unknown") is False
    assert runner_supported("docker", "Docker 1") is True


def test_mutable_image_is_pulled_before_digest_resolution(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    digest = "docker.io/library/python@sha256:" + "a" * 64

    def fake_run(command, **kwargs):
        args = tuple(command)
        calls.append(args)
        if args[1:3] == ("image", "pull"):
            return Completed(args, 0, "pulled", "")
        return Completed(args, 0, f'[{{"RepoDigests":["{digest}"]}}]', "")

    monkeypatch.setattr(runners, "run", fake_run)

    assert runners.resolve_image_identity("docker", "python:3.13") == digest
    assert [call[1:3] for call in calls] == [
        ("image", "pull"),
        ("image", "inspect"),
    ]


def test_exact_image_digest_uses_present_local_content_without_pull(
    monkeypatch,
) -> None:
    image = "docker.io/library/python@sha256:" + "a" * 64
    calls: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        args = tuple(command)
        calls.append(args)
        return Completed(args, 0, "[]", "")

    monkeypatch.setattr(runners, "run", fake_run)

    assert runners.resolve_image_identity("docker", image) == image
    assert [call[1:3] for call in calls] == [("image", "inspect")]


def test_mutable_image_never_falls_back_to_stale_local_tag(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        args = tuple(command)
        return Completed(args, 1, "", "registry unavailable")

    monkeypatch.setattr(runners, "run", fake_run)

    with pytest.raises(runners.RunnerError, match="failed to pull image"):
        runners.resolve_image_identity("docker", "python:3.13")


def test_runner_rejects_option_shaped_image_before_process_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        runners,
        "run",
        lambda *args, **kwargs: pytest.fail("unsafe image must not reach a runner"),
    )

    with pytest.raises(runners.RunnerError, match="safe OCI reference"):
        runners.resolve_image_identity("docker", "--privileged")
    with pytest.raises(runners.RunnerError, match="safe OCI reference"):
        build_runner_command(
            "docker",
            CheckStep("custom", "--privileged", "true"),
            tmp_path,
            cpus=2,
            memory="4g",
        )


def test_runner_rejects_malformed_reported_digest(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        args = tuple(command)
        if args[1:3] == ("image", "pull"):
            return Completed(args, 0, "pulled", "")
        return Completed(
            args,
            0,
            '[{"RepoDigests":["python@sha256:not-a-digest"]}]',
            "",
        )

    monkeypatch.setattr(runners, "run", fake_run)

    with pytest.raises(runners.RunnerError, match="did not report a content digest"):
        runners.resolve_image_identity("docker", "python:3.13")


@pytest.mark.parametrize(
    ("version", "ready", "expected"),
    [
        ("container 1.0.0", True, "container"),
        ("container 0.12.3", True, "docker"),
        ("container 1.0.0", False, "docker"),
    ],
)
def test_macos_auto_uses_only_supported_ready_apple_container(
    monkeypatch,
    version: str,
    ready: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(runner_policy.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        runner_policy.shutil,
        "which",
        lambda name: name if name in {"container", "docker"} else None,
    )
    monkeypatch.setattr(runner_policy, "runner_version", lambda name: version)
    monkeypatch.setattr(
        runner_policy,
        "runner_ready",
        lambda name: ready if name == "container" else True,
    )

    assert select_runner("auto") == expected


def test_auto_skips_stopped_docker_for_ready_podman(monkeypatch) -> None:
    monkeypatch.setattr(runner_policy.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        runner_policy.shutil,
        "which",
        lambda name: name if name in {"docker", "podman"} else None,
    )
    monkeypatch.setattr(
        runner_policy,
        "runner_ready",
        lambda name: name == "podman",
    )

    assert select_runner("auto") == "podman"


def test_cache_preference_does_not_probe_daemons(monkeypatch) -> None:
    monkeypatch.setattr(runner_policy.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        runner_policy.shutil,
        "which",
        lambda name: name if name in {"docker", "podman"} else None,
    )
    monkeypatch.setattr(
        runner_policy,
        "runner_ready",
        lambda name: pytest.fail("cache preference must not probe a daemon"),
    )

    assert preferred_runner("auto") == "docker"


def test_explicit_apple_container_rejects_unsupported_version(monkeypatch) -> None:
    monkeypatch.setattr(runner_policy.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(runner_policy.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        runner_policy,
        "runner_version",
        lambda name: "container 0.12.3",
    )

    with pytest.raises(runners.RunnerError, match="1.0.0 or newer"):
        select_runner("container")


def test_test_phase_can_disable_network_on_every_runner(tmp_path: Path) -> None:
    docker = build_runner_command(
        "docker",
        _step(),
        tmp_path,
        cpus=2,
        memory="4g",
        network_enabled=False,
    )
    apple = build_runner_command(
        "container",
        _step(),
        tmp_path,
        cpus=2,
        memory="4g",
        network_enabled=False,
    )

    assert "--network=none" in docker
    network_index = apple.index("--network")
    assert apple[network_index + 1] == "none"


def test_node_prepared_volume_is_mounted_at_node_modules(tmp_path: Path) -> None:
    step = CheckStep(
        "node",
        "node:24-bookworm",
        "npm test",
        ("package.json",),
        prepare_command="npm ci",
        test_network="none",
        working_directory="apps/web",
    )

    command = build_runner_command(
        "docker",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        prepared_volume="ghfc-test",
        support_volume="ghfc-test-support",
    )

    assert "--volume=ghfc-test:/workspace/apps/web/node_modules" in command
    assert (
        "--mount=type=volume,source=ghfc-test-support,target=/prepared"
        in command
    )
    assert "cd /workspace/apps/web && npm test" in command[-1]


def test_apple_container_carries_node_support_and_offline_policy(
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "node",
        "node:24-bookworm",
        "/prepared/corepack/node_modules/.bin/corepack pnpm test",
        ("package.json",),
        prepare_command="corepack pnpm install",
        test_network="none",
    )

    command = build_runner_command(
        "container",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        prepared_volume="ghfc-test",
        support_volume="ghfc-test-support",
        network_enabled=False,
    )

    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"
    volumes = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--volume"
    ]
    assert "ghfc-test:/workspace/node_modules" in volumes
    assert "ghfc-test-support:/prepared" in volumes


def test_python_cache_stays_on_linux_native_prepared_volume(tmp_path: Path) -> None:
    step = CheckStep(
        "python",
        "python:3.13",
        ".venv/bin/python -m pytest",
        ("pyproject.toml",),
        prepare_command="uv sync",
        test_network="none",
    )

    command = build_runner_command(
        "docker",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        cache_dir=tmp_path / "host-cache",
        prepared_volume="ghfc-test",
    )
    rendered = " ".join(command)

    assert "--env=UV_CACHE_DIR=/prepared/uv" in command
    assert "--env=PIP_CACHE_DIR=/prepared/pip" in command
    assert "source=ghfc-test,target=/prepared" in rendered
    assert "target=/cache" not in rendered


def test_deno_vendor_state_is_carried_between_phase_containers(
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "deno",
        "denoland/deno:debian",
        "deno task --frozen test",
        ("deno.json",),
        prepare_command="deno install --frozen",
    )

    command = build_runner_command(
        "docker",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        prepared_volume="ghfc-deno",
    )

    assert (
        "--mount=type=volume,source=ghfc-deno,target=/prepared"
        in command
    )
    assert "--env=DENO_DIR=/prepared/deno" in command
    assert "ln -s /prepared/vendor /workspace/vendor" in command[-1]
    assert "ln -s /prepared/node_modules /workspace/node_modules" in command[-1]


def test_python_nested_working_directory_uses_persistent_environment(
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "python",
        "python:3.13",
        ".venv/bin/python -m pytest",
        ("services/api/pyproject.toml",),
        prepare_command="python -m venv .venv",
        test_network="none",
        working_directory="services/api",
    )

    command = build_runner_command(
        "docker",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        prepared_volume="ghfc-test",
    )
    rendered = " ".join(command)

    assert "target=/prepared" in rendered
    assert "ln -s /prepared/venv /workspace/services/api/.venv" in command[-1]
    assert "cd /workspace/services/api && .venv/bin/python -m pytest" in command[-1]


def test_run_step_uses_distinct_prepare_and_offline_test_containers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "go",
        "golang:1.24-bookworm",
        "GOPROXY=off go test ./...",
        ("go.mod",),
        prepare_command="go mod download",
        test_network="none",
    )
    invocations: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: "golang@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")

    def fake_execute_phase(**kwargs):
        invocations.append((kwargs["container_name"], kwargs["command"]))
        return runners._PhaseExecution(0, 0.1, "", frozenset())

    monkeypatch.setattr(runners, "_execute_phase", fake_execute_phase)

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "run.log",
        cpus=2,
        memory="4g",
        echo=False,
    )

    assert result.returncode == 0
    assert result.prepare_duration_seconds == 0.1
    assert result.prepare_cache_hit is False
    assert len(invocations) == 2
    assert invocations[0][0].endswith("-prepare")
    assert invocations[1][0].endswith("-test")
    assert "go mod download" in invocations[0][1][-1]
    assert "--network=none" not in invocations[0][1]
    assert "GOPROXY=off go test ./..." in invocations[1][1][-1]
    assert "--network=none" in invocations[1][1]


def test_run_step_records_verified_prepare_cache_hit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "go",
        "golang:1.24-bookworm",
        "GOPROXY=off go test ./...",
        ("go.mod",),
        prepare_command="go mod download",
        test_network="none",
    )
    invocations: list[list[str]] = []
    details = iter((runners._PREPARE_CACHE_HIT, ""))

    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: "golang@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")

    def fake_execute_phase(**kwargs):
        invocations.append(kwargs["command"])
        return runners._PhaseExecution(0, 0.1, next(details), frozenset())

    monkeypatch.setattr(runners, "_execute_phase", fake_execute_phase)

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "run.log",
        cpus=2,
        memory="4g",
        echo=False,
        cache_dir=tmp_path / "cache",
    )

    assert result.prepare_cache_hit is True
    assert "test -f /cache/.gh-freshclone-prepared-v1" in invocations[0][-1]
    assert "touch /cache/.gh-freshclone-prepared-v1" in invocations[0][-1]


def test_phase_log_is_bounded_while_process_output_is_drained(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runners, "MAX_LOG_BYTES", 256)
    log_path = tmp_path / "bounded.log"

    result = runners._execute_phase(
        runner="docker",
        command=[sys.executable, "-c", "print('x' * 4096)"],
        container_name="unused",
        log_path=log_path,
        phase="test",
        echo=False,
    )

    assert result.returncode == 0
    assert log_path.stat().st_size <= 256
    assert "output truncated at 256 bytes" in log_path.read_text(encoding="utf-8")


def test_exit_classification() -> None:
    assert classify_exit(0, "") == "pass"
    assert classify_exit(125, "daemon unavailable") == "infra_failure"
    assert classify_exit(127, "cargo: command not found") == "environment_gap"
    assert classify_exit(1, "PermissionError: tool cannot execute") == "environment_gap"
    assert classify_exit(1, "assertion failed") == "test_failure"


def test_network_policy_failure_is_actionable() -> None:
    status, diagnostics = runners.diagnose_failure(
        1,
        "connect: network is unreachable",
        test_network="none",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "network_policy"


def test_prepare_cache_failure_is_infrastructure() -> None:
    status, diagnostics = runners.diagnose_failure(
        1,
        "Failed to read from the distribution cache: failed to rename file",
        failed_phase="prepare",
    )

    assert status == "infra_failure"
    assert diagnostics[0].kind == "dependency_preparation_infrastructure"


def test_console_output_is_safe_for_windows_code_pages() -> None:
    value = _console_safe("skipped ⚠\n", "cp932")

    value.encode("cp932")
    assert "skipped" in value


def test_failure_selector_yields_only_known_executable_candidates() -> None:
    assert failure_executable_candidates(
        "FAILED tests/test_pager.py::test_output[test0-less]"
    ) == ("less",)
    assert failure_executable_candidates(
        "PASSED tests/test_pager.py::test_output[test0-less]"
    ) == ()


def test_runner_readiness_checks_daemon_not_only_executable(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(runner_policy.shutil, "which", lambda name: name)

    def fake_run(command, **kwargs):
        commands.append(tuple(command))
        return Completed(tuple(command), 0, "", "")

    monkeypatch.setattr(runner_policy, "_run", fake_run)

    assert runner_ready("docker") is True
    assert runner_ready("container") is True
    assert commands == [
        ("docker", "info"),
        ("container", "system", "status"),
    ]
