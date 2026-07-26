from __future__ import annotations

import sys
import threading
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
    assert "--label=gh-freshclone.managed=true" in command
    assert "--label=gh-freshclone.kind=execution" in command
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
    assert command[command.index("--tmpfs") + 1] == "/tmp"
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
    calls: list[tuple[tuple[str, ...], float | None]] = []
    digest = "docker.io/library/python@sha256:" + "a" * 64

    def fake_run(command, **kwargs):
        args = tuple(command)
        calls.append((args, kwargs.get("timeout")))
        if args[1:3] == ("image", "pull"):
            return Completed(args, 0, "pulled", "")
        return Completed(args, 0, f'[{{"RepoDigests":["{digest}"]}}]', "")

    monkeypatch.setattr(runners, "run", fake_run)

    assert runners.resolve_image_identity("docker", "python:3.13") == digest
    assert [(call[0][1:3], call[1]) for call in calls] == [
        (("image", "pull"), None),
        (("image", "inspect"), 15),
    ]


def test_exact_image_digest_uses_present_local_content_without_pull(
    monkeypatch,
) -> None:
    image = "docker.io/library/python@sha256:" + "a" * 64
    calls: list[tuple[tuple[str, ...], float | None]] = []

    def fake_run(command, **kwargs):
        args = tuple(command)
        calls.append((args, kwargs.get("timeout")))
        return Completed(args, 0, "[]", "")

    monkeypatch.setattr(runners, "run", fake_run)

    assert runners.resolve_image_identity("docker", image) == image
    assert [(call[0][1:3], call[1]) for call in calls] == [
        (("image", "inspect"), 15),
    ]


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


def test_auto_runner_readiness_probes_are_concurrent(monkeypatch) -> None:
    barrier = threading.Barrier(2)
    monkeypatch.setattr(runner_policy.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        runner_policy.shutil,
        "which",
        lambda name: name if name in {"docker", "podman"} else None,
    )

    def ready(name: str) -> bool:
        barrier.wait(timeout=1)
        return name == "podman"

    monkeypatch.setattr(runner_policy, "runner_ready", ready)

    assert select_runner("auto") == "podman"


def test_installed_runner_version_probes_are_concurrent(monkeypatch) -> None:
    barrier = threading.Barrier(2)
    monkeypatch.setattr(
        runner_policy.shutil,
        "which",
        lambda name: name if name in {"docker", "podman"} else None,
    )

    def probe(command, **kwargs):
        barrier.wait(timeout=1)
        args = tuple(command)
        return Completed(args, 0, f"{args[0]} 1.0.0", "")

    monkeypatch.setattr(runner_policy, "_run", probe)

    assert runner_policy.available_runners() == {
        "docker": "docker 1.0.0",
        "podman": "podman 1.0.0",
        "container": None,
    }


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


@pytest.mark.parametrize(
    ("ecosystem", "environment"),
    [
        ("maven", "--env=MAVEN_USER_HOME=/cache/maven-home"),
        ("gradle", "--env=GRADLE_USER_HOME=/cache/gradle"),
    ],
)
def test_java_build_tools_use_scoped_named_volume(
    tmp_path: Path,
    ecosystem: str,
    environment: str,
) -> None:
    step = CheckStep(
        ecosystem,
        "eclipse-temurin:21-jdk-noble",
        "test command",
        ("build manifest",),
        prepare_command="prepare command",
        test_network="none",
    )
    cache = tmp_path / "dependency-cache"

    command = build_runner_command(
        "docker",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        cache_dir=cache,
        prepared_volume="ghfc-java-cache",
    )

    assert environment in command
    assert (
        "--mount=type=volume,source=ghfc-java-cache,target=/cache"
        in command
    )
    assert (
        f"--mount=type=bind,source={cache.resolve()},target=/cache"
        not in command
    )
    assert "target=/prepared" not in " ".join(command)


@pytest.mark.parametrize("ecosystem", ["maven", "gradle"])
def test_java_preparation_cache_requires_success_marker(
    tmp_path: Path,
    ecosystem: str,
) -> None:
    markers = runners._prepare_marker_paths(
        CheckStep(
            ecosystem,
            "eclipse-temurin:21-jdk-noble",
            "test command",
            prepare_command="prepare command",
        ),
        effective_cache=None,
        effective_volume="ghfc-java-cache",
        support_volume=None,
    )

    assert markers == ("/cache/.gh-freshclone-prepared-v1",)


@pytest.mark.parametrize("ecosystem", ["maven", "gradle"])
def test_java_preparation_uses_image_scoped_managed_volume(
    monkeypatch,
    tmp_path: Path,
    ecosystem: str,
) -> None:
    step = CheckStep(
        ecosystem,
        "eclipse-temurin:21-jdk-noble",
        "test command",
        prepare_command="prepare command",
        test_network="none",
    )
    created: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: f"{image}@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")
    monkeypatch.setattr(
        runners,
        "_ensure_prepared_volume",
        lambda runner, name: created.append(name),
    )
    monkeypatch.setattr(
        runners,
        "_execute_phase",
        lambda **kwargs: (
            commands.append(kwargs["command"])
            or runners._PhaseExecution(0, 0.1, "", frozenset())
        ),
    )

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / f"{ecosystem}.log",
        cpus=2,
        memory="4g",
        echo=False,
        cache_dir=tmp_path / "host-cache",
        prepared_volume=f"ghfc-{ecosystem}",
    )

    assert created == [result.prepared_volume]
    assert result.prepared_volume.startswith(f"ghfc-{ecosystem}-i")
    assert all(
        f"source={result.prepared_volume},target=/cache" in " ".join(command)
        for command in commands
    )
    host_cache = str((tmp_path / "host-cache").resolve())
    assert all(host_cache not in command for command in commands)


@pytest.mark.parametrize("ecosystem", ["maven", "gradle"])
def test_apple_container_mounts_java_managed_volume(
    tmp_path: Path,
    ecosystem: str,
) -> None:
    step = CheckStep(
        ecosystem,
        "eclipse-temurin:21-jdk-noble",
        "test command",
        prepare_command="prepare command",
        test_network="none",
    )

    command = build_runner_command(
        "container",
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        cache_dir=tmp_path / "host-cache",
        prepared_volume="ghfc-java-cache",
    )
    volumes = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--volume"
    ]

    assert "ghfc-java-cache:/cache" in volumes
    assert all("host-cache" not in volume for volume in volumes)


@pytest.mark.parametrize("runner", ["docker", "container"])
def test_cmake_tools_use_scoped_prepared_volume(
    tmp_path: Path,
    runner: str,
) -> None:
    step = CheckStep(
        "cmake",
        "docker.io/library/python:3.13-bookworm",
        "export PATH=/prepared/tools/bin:$PATH && cmake --version",
        ("CMakeLists.txt",),
        prepare_command="python -m pip install --target /prepared/tools cmake ninja",
        test_network="none",
    )
    cache = tmp_path / "host-cache"

    command = build_runner_command(
        runner,
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        cache_dir=cache,
        prepared_volume="ghfc-cmake-cache",
        network_enabled=False,
    )
    rendered = " ".join(command)

    assert "ghfc-cmake-cache" in rendered
    assert "/prepared" in rendered
    assert str(cache.resolve()) not in rendered
    assert "PIP_CACHE_DIR=/prepared/pip" in rendered
    assert "PYTHONPATH=/prepared/tools" in rendered
    if runner == "container":
        assert command[command.index("--network") + 1] == "none"
    else:
        assert "--network=none" in command


def test_cmake_preparation_cache_requires_success_marker() -> None:
    markers = runners._prepare_marker_paths(
        CheckStep(
            "cmake",
            "docker.io/library/python:3.13-bookworm",
            "ctest",
            prepare_command="install tools",
        ),
        effective_cache=None,
        effective_volume="ghfc-cmake-cache",
        support_volume=None,
    )

    assert markers == ("/prepared/.gh-freshclone-prepared-v1",)


@pytest.mark.parametrize("runner", ["docker", "container"])
def test_dotnet_restore_state_uses_scoped_prepared_volume(
    tmp_path: Path,
    runner: str,
) -> None:
    step = CheckStep(
        "dotnet",
        "mcr.microsoft.com/dotnet/sdk:10.0",
        "dotnet test Product.sln --no-restore",
        ("Product.sln",),
        prepare_command="dotnet restore Product.sln",
        test_network="none",
    )
    cache = tmp_path / "host-cache"

    command = build_runner_command(
        runner,
        step,
        tmp_path,
        cpus=2,
        memory="4g",
        cache_dir=cache,
        prepared_volume="ghfc-dotnet-cache",
        network_enabled=False,
    )
    rendered = " ".join(command)

    assert "ghfc-dotnet-cache" in rendered
    assert "/prepared" in rendered
    assert str(cache.resolve()) not in rendered
    assert "NUGET_PACKAGES=/prepared/nuget" in rendered
    assert "DOTNET_CLI_HOME=/prepared/home" in rendered
    assert "DOTNET_CLI_TELEMETRY_OPTOUT=1" in rendered
    if runner == "container":
        assert command[command.index("--network") + 1] == "none"
    else:
        assert "--network=none" in command


def test_dotnet_preparation_cache_requires_success_marker() -> None:
    markers = runners._prepare_marker_paths(
        CheckStep(
            "dotnet",
            "mcr.microsoft.com/dotnet/sdk:10.0",
            "dotnet test Product.sln --no-restore",
            prepare_command="dotnet restore Product.sln",
        ),
        effective_cache=None,
        effective_volume="ghfc-dotnet-cache",
        support_volume=None,
    )

    assert markers == ("/prepared/.gh-freshclone-prepared-v1",)


def test_cmake_preparation_uses_image_scoped_managed_volume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "cmake",
        "docker.io/library/python:3.13-bookworm",
        "ctest",
        prepare_command="install tools",
        test_network="none",
    )
    created: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: f"{image}@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")
    monkeypatch.setattr(
        runners,
        "_ensure_prepared_volume",
        lambda runner, name: created.append(name),
    )
    monkeypatch.setattr(
        runners,
        "_execute_phase",
        lambda **kwargs: (
            commands.append(kwargs["command"])
            or runners._PhaseExecution(0, 0.1, "", frozenset())
        ),
    )

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "cmake.log",
        cpus=2,
        memory="4g",
        echo=False,
        cache_dir=tmp_path / "host-cache",
        prepared_volume="ghfc-cmake",
    )

    assert created == [result.prepared_volume]
    assert result.prepared_volume.startswith("ghfc-cmake-i")
    assert all(
        f"source={result.prepared_volume},target=/prepared" in " ".join(command)
        for command in commands
    )
    assert all("--network=none" not in command for command in commands[:1])
    assert "--network=none" in commands[1]
    host_cache = str((tmp_path / "host-cache").resolve())
    assert all(host_cache not in command for command in commands)


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


def test_failed_preparation_retries_once_from_a_clean_prepared_volume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "deno",
        "denoland/deno:debian",
        "deno task --frozen test",
        ("deno.json",),
        prepare_command="deno install --frozen",
        test_network="none",
    )
    discarded: list[tuple[str, str]] = []
    phases = iter(
        (
            runners._PhaseExecution(
                1,
                0.1,
                "dependency cache is corrupt",
                frozenset(),
            ),
            runners._PhaseExecution(0, 0.1, "", frozenset()),
            runners._PhaseExecution(0, 0.1, "", frozenset()),
        )
    )
    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: "denoland/deno@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")
    monkeypatch.setattr(runners, "_ensure_prepared_volume", lambda *args: None)
    monkeypatch.setattr(
        runners,
        "_execute_phase",
        lambda **kwargs: next(phases),
    )
    monkeypatch.setattr(
        runners,
        "discard_prepared_volume",
        lambda runner, name: (
            discarded.append((runner, name)) or True,
            None,
        ),
    )
    monkeypatch.setattr(
        runners,
        "discard_dependency_cache",
        lambda path: pytest.fail("Deno must use only its prepared volume"),
    )

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "prepare-failed.log",
        cpus=2,
        memory="4g",
        echo=False,
        prepared_volume="ghfc-12345678-123456789abc-quick-deno-p6-e16",
    )

    assert result.returncode == 0
    assert result.failed_phase is None
    assert len(discarded) == 1
    assert discarded[0][0] == "docker"
    assert discarded[0][1].startswith(
        "ghfc-12345678-123456789abc-quick-deno-p6-e16-i"
    )
    assert "retried preparation once from a clean scoped cache" in result.detail
    assert "discarded 1 scoped cache resources" in (
        tmp_path / "prepare-failed.log"
    ).read_text(encoding="utf-8")


def test_repeated_prepare_failure_retries_only_once_and_leaves_clean_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "deno",
        "denoland/deno:debian",
        "deno task --frozen test",
        ("deno.json",),
        prepare_command="deno install --frozen",
        test_network="none",
    )
    phase_calls = 0
    discarded: list[str] = []

    def fail_prepare(**kwargs):
        nonlocal phase_calls
        phase_calls += 1
        return runners._PhaseExecution(1, 0.1, "still corrupt", frozenset())

    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: "denoland/deno@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")
    monkeypatch.setattr(runners, "_ensure_prepared_volume", lambda *args: None)
    monkeypatch.setattr(runners, "_execute_phase", fail_prepare)
    monkeypatch.setattr(
        runners,
        "discard_prepared_volume",
        lambda runner, name: (discarded.append(name) or True, None),
    )

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "prepare-failed-twice.log",
        cpus=2,
        memory="4g",
        echo=False,
        prepared_volume="ghfc-12345678-123456789abc-quick-deno-p6-e16",
    )

    assert result.failed_phase == "prepare"
    assert phase_calls == 2
    assert len(discarded) == 2
    assert "after the clean retry" in result.detail


def test_failed_preparation_retries_from_a_clean_host_cache(
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
    phases = iter(
        (
            runners._PhaseExecution(1, 0.1, "corrupt module cache", frozenset()),
            runners._PhaseExecution(0, 0.1, "", frozenset()),
            runners._PhaseExecution(0, 0.1, "", frozenset()),
        )
    )
    discarded: list[Path] = []
    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: "golang@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")
    monkeypatch.setattr(runners, "_execute_phase", lambda **kwargs: next(phases))
    monkeypatch.setattr(
        runners,
        "discard_dependency_cache",
        lambda path: (discarded.append(path) or True, None),
    )
    monkeypatch.setattr(
        runners,
        "discard_prepared_volume",
        lambda *args: pytest.fail("Go must use only its host cache"),
    )

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "host-prepare-failed.log",
        cpus=2,
        memory="4g",
        echo=False,
        cache_dir=tmp_path / "runner-cache" / "placeholder",
    )

    assert result.returncode == 0
    assert len(discarded) == 1
    assert discarded[0].parent == tmp_path / "runner-cache"
    assert discarded[0].is_dir()
    assert "retried preparation once from a clean scoped cache" in result.detail


def test_test_failure_preserves_successfully_prepared_dependencies(
    monkeypatch,
    tmp_path: Path,
) -> None:
    step = CheckStep(
        "deno",
        "denoland/deno:debian",
        "deno task --frozen test",
        ("deno.json",),
        prepare_command="deno install --frozen",
        test_network="none",
    )
    phases = iter(
        (
            runners._PhaseExecution(0, 0.1, "", frozenset()),
            runners._PhaseExecution(1, 0.1, "assertion failed", frozenset()),
        )
    )
    monkeypatch.setattr(
        runners,
        "resolve_image_identity",
        lambda runner, image: "denoland/deno@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(runners, "runner_version", lambda runner: "test")
    monkeypatch.setattr(runners, "_ensure_prepared_volume", lambda *args: None)
    monkeypatch.setattr(runners, "_execute_phase", lambda **kwargs: next(phases))
    monkeypatch.setattr(
        runners,
        "discard_prepared_volume",
        lambda *args: pytest.fail("test failure must retain prepared dependencies"),
    )

    result = run_step(
        "docker",
        step,
        tmp_path,
        tmp_path / "test-failed.log",
        cpus=2,
        memory="4g",
        echo=False,
        prepared_volume="ghfc-12345678-123456789abc-quick-deno-p6-e16",
    )

    assert result.failed_phase == "test"
    assert "discarded" not in result.detail


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


def test_phase_detail_preserves_early_gradle_network_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runners, "_stop_execution", lambda *args: None)
    script = (
        "print('java.net.UnknownHostException: services.gradle.org'); "
        "[print(f'ordinary output {index}') for index in range(30)]; "
        "raise SystemExit(1)"
    )

    result = runners._execute_phase(
        runner="docker",
        command=[sys.executable, "-c", script],
        container_name="unused",
        log_path=tmp_path / "network-failure.log",
        phase="test",
        echo=False,
    )

    assert result.returncode == 1
    assert "--- diagnostic evidence ---" in result.detail
    assert "UnknownHostException: services.gradle.org" in result.detail


def test_nonzero_runner_exit_attempts_named_container_cleanup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner_commands: list[tuple[tuple[str, ...], float | None]] = []
    monkeypatch.setattr(
        runners,
        "run",
        lambda command, **kwargs: (
            runner_commands.append((tuple(command), kwargs.get("timeout")))
            or Completed(tuple(command), 0, "", "")
        ),
    )

    result = runners._execute_phase(
        runner="docker",
        command=[sys.executable, "-c", "raise SystemExit(125)"],
        container_name="gh-freshclone-failed-prepare",
        log_path=tmp_path / "failed.log",
        phase="prepare",
        echo=False,
    )

    assert result.returncode == 125
    assert runner_commands == [
        (
            ("docker", "rm", "--force", "gh-freshclone-failed-prepare"),
            15,
        ),
    ]


def test_default_sigterm_cleans_up_named_container_before_exit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    signal_handlers: list[object] = []
    runner_commands: list[tuple[str, ...]] = []

    def install_signal(_signum, handler):
        signal_handlers.append(handler)

    monkeypatch.setattr(runners.signal, "getsignal", lambda _signum: runners.signal.SIG_DFL)
    monkeypatch.setattr(runners.signal, "signal", install_signal)
    monkeypatch.setattr(
        runners,
        "run",
        lambda command, **_kwargs: (
            runner_commands.append(tuple(command))
            or Completed(tuple(command), 0, "", "")
        ),
    )

    class SignalOutput:
        def readline(self, _size: int) -> str:
            handler = signal_handlers[0]
            assert callable(handler)
            handler(runners.signal.SIGTERM, None)
            raise AssertionError("SIGTERM handler must exit")

    class RunningProcess:
        stdout = SignalOutput()
        terminated = False

        def poll(self):
            return None if not self.terminated else 0

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self.terminated = True

    process = RunningProcess()
    monkeypatch.setattr(runners.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(SystemExit) as interrupted:
        runners._execute_phase(
            runner="container",
            command=["container", "run", "image"],
            container_name="gh-freshclone-sigterm-test",
            log_path=tmp_path / "sigterm.log",
            phase="test",
            echo=False,
        )

    assert interrupted.value.code == 128 + runners.signal.SIGTERM
    assert process.terminated is True
    assert runner_commands == [
        ("container", "stop", "gh-freshclone-sigterm-test"),
        ("container", "delete", "gh-freshclone-sigterm-test"),
    ]
    assert signal_handlers[-1] == runners.signal.SIG_DFL


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


def test_runner_control_probe_has_a_bounded_deadline(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = tuple(command)
        observed.update(kwargs)
        return Completed(tuple(command), 0, "", "")

    monkeypatch.setattr("gh_freshclone.process.run", fake_run)

    runner_policy._run(["docker", "info"], check=False)

    assert observed == {
        "command": ("docker", "info"),
        "check": False,
        "timeout": 15,
    }


def test_prepared_volume_control_calls_have_bounded_deadlines(monkeypatch) -> None:
    observed: list[tuple[tuple[str, ...], float | None]] = []

    def fake_run(command, **kwargs):
        args = tuple(command)
        observed.append((args, kwargs.get("timeout")))
        if args[1:3] == ("volume", "inspect"):
            return Completed(args, 1, "", "no such volume")
        return Completed(args, 0, "created", "")

    monkeypatch.setattr(runners, "run", fake_run)
    monkeypatch.setattr(runners, "record_prepared_volume", lambda *args: None)

    runners._ensure_prepared_volume(
        "docker",
        "ghfc-12345678-123456789abc-quick-python-p6-e16",
    )

    assert [timeout for _, timeout in observed] == [15, 15]


def test_failure_diagnostic_probe_has_a_bounded_deadline(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = tuple(command)
        observed.update(kwargs)
        return Completed(tuple(command), 0, "less\n", "")

    monkeypatch.setattr(runners, "run", fake_run)

    missing = runners._probe_missing_executables(
        "docker",
        "python@sha256:" + "a" * 64,
        {"less"},
    )

    assert missing == ("less",)
    assert observed["check"] is False
    assert observed["timeout"] == 15
