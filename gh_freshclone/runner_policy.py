from __future__ import annotations

import platform
import re
import shutil

from .constants import APPLE_CONTAINER_MIN_VERSION
from .model import ResourceLimits

SUPPORTED_RUNNERS = ("docker", "podman", "container")


class RunnerError(RuntimeError):
    pass


def _run(command: list[str], *, check: bool = False):
    from .process import run

    return run(command, check=check)


def available_runners() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in SUPPORTED_RUNNERS:
        executable = shutil.which(name)
        if not executable:
            result[name] = None
            continue
        version = _run([name, "--version"], check=False)
        output = (version.stdout or version.stderr).strip().splitlines()
        result[name] = output[0] if output else executable
    return result


def runner_ready(runner: str) -> bool:
    if shutil.which(runner) is None:
        return False
    command = (
        ["container", "system", "status"]
        if runner == "container"
        else [runner, "info"]
    )
    return _run(command, check=False).returncode == 0


def runner_version(runner: str) -> str:
    executable = shutil.which(runner)
    if not executable:
        return "unknown"
    version = _run([runner, "--version"], check=False)
    output = (version.stdout or version.stderr).strip().splitlines()
    return output[0] if output else executable


def _semantic_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def runner_supported(runner: str, version: str | None) -> bool:
    if runner != "container":
        return True
    parsed = _semantic_version(version or "")
    return parsed is not None and parsed >= APPLE_CONTAINER_MIN_VERSION


def _apple_requirement() -> str:
    return ".".join(map(str, APPLE_CONTAINER_MIN_VERSION))


def _runner_preferences(requested: str) -> tuple[str, ...]:
    if requested != "auto":
        if requested not in SUPPORTED_RUNNERS:
            raise RunnerError(f"unknown runner: {requested}")
        if shutil.which(requested) is None:
            raise RunnerError(f"requested runner is not installed: {requested}")
        if requested == "container" and platform.system() != "Darwin":
            raise RunnerError("Apple container runner is supported only on macOS")
        if requested == "container":
            version = runner_version("container")
            if not runner_supported("container", version):
                raise RunnerError(
                    f"Apple container {_apple_requirement()} or newer is required; "
                    f"found {version}"
                )
        return (requested,)

    apple_problem: str | None = None
    candidates: list[str] = []
    if platform.system() == "Darwin" and shutil.which("container"):
        version = runner_version("container")
        if not runner_supported("container", version):
            apple_problem = (
                f"Apple container {_apple_requirement()} or newer is required; "
                f"found {version}"
            )
        else:
            candidates.append("container")

    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            candidates.append(candidate)
    if candidates:
        return tuple(candidates)
    if apple_problem:
        raise RunnerError(apple_problem)
    raise RunnerError(
        "no OCI runner found; install Docker, Podman, or Apple container on macOS"
    )


def preferred_runner(requested: str) -> str:
    """Return the cache preference without starting or probing any daemon."""

    return _runner_preferences(requested)[0]


def select_runner(requested: str) -> str:
    """Select a ready runner after pre-clone PASS lookup has missed."""

    candidates = _runner_preferences(requested)
    for candidate in candidates:
        if runner_ready(candidate):
            return candidate
    hint = (
        " run `container system start` or start Docker/Podman"
        if "container" in candidates
        else " start Docker/Podman"
    )
    raise RunnerError(
        f"no installed OCI runner is ready;{hint} and run `gh-freshclone doctor`"
    )


def validate_runner_limits(runner: str, cpus: float, memory: str) -> None:
    try:
        limits = ResourceLimits(cpus=cpus, memory=memory)
    except (TypeError, ValueError) as exc:
        raise RunnerError(str(exc)) from exc
    if runner == "container" and not limits.cpus.is_integer():
        raise RunnerError("Apple container requires a whole-number CPU limit")
