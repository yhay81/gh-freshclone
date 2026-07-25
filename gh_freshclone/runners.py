from __future__ import annotations

import hashlib
import json
import re
import shlex
import signal
import subprocess  # nosec B404
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from . import runner_policy as _runner_policy
from .cache import (
    cache_path_lock,
    cache_volume_lock,
    discard_dependency_cache,
    discard_prepared_volume,
    record_prepared_volume,
    touch_dependency_cache,
)
from .constants import RUNNER_CONTROL_TIMEOUT_SECONDS
from .diagnostics import diagnose_failure, failure_executable_candidates
from .model import CheckStep
from .process import run
from .receipts import cache_namespace, execution_cache_key

RunnerError = _runner_policy.RunnerError
available_runners = _runner_policy.available_runners
preferred_runner = _runner_policy.preferred_runner
runner_ready = _runner_policy.runner_ready
runner_supported = _runner_policy.runner_supported
runner_version = _runner_policy.runner_version
select_runner = _runner_policy.select_runner
validate_runner_limits = _runner_policy.validate_runner_limits

MAX_LOG_BYTES = 10 * 1024 * 1024
_PREPARE_CACHE_HIT = "gh-freshclone: verified preparation cache hit"
_PREPARE_MARKER = ".gh-freshclone-prepared-v1"
_CONTAINER_TMP = str(PurePosixPath("/", "tmp"))
_OCI_IMAGE_REFERENCE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]*"
    r"(?:@[A-Za-z0-9_+.-]+:[A-Fa-f0-9]+)?$"
)
_RESOLVED_IMAGE_REFERENCE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$"
)


@dataclass(frozen=True)
class RunnerExecution:
    returncode: int
    duration_seconds: float
    detail: str
    image_identity: str = ""
    runner_version: str = ""
    observed_missing_executables: tuple[str, ...] = ()
    dependency_cache: str = ""
    prepared_volume: str = ""
    prepare_duration_seconds: float = 0
    test_network: str = "enabled"
    failed_phase: str | None = None
    prepare_cache_hit: bool | None = None


@dataclass(frozen=True)
class _PhaseExecution:
    returncode: int
    duration_seconds: float
    detail: str
    failure_candidates: frozenset[str]


def _install_cleanup_sigterm() -> Any | None:
    """Turn the default SIGTERM action into a cleanup-capable process exit."""

    if threading.current_thread() is not threading.main_thread():
        return None
    previous = signal.getsignal(signal.SIGTERM)
    if previous != signal.SIG_DFL:
        return None

    def terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    return previous


def _restore_sigterm(previous: Any | None) -> None:
    if previous is not None:
        signal.signal(signal.SIGTERM, previous)


def _console_safe(value: str, encoding: str | None) -> str:
    """Replace characters a redirected Windows console cannot encode."""

    selected = encoding or "utf-8"
    try:
        return value.encode(selected, errors="replace").decode(selected)
    except LookupError:
        return value


def _repository_without_tag(image: str) -> str:
    if "@" in image:
        return image.split("@", 1)[0]
    head, separator, tail = image.rpartition("/")
    if ":" in tail:
        tail = tail.split(":", 1)[0]
    return f"{head}{separator}{tail}" if separator else tail


def _validate_image_reference(image: str) -> None:
    if (
        not image
        or len(image) > 512
        or any(character.isspace() for character in image)
        or "\0" in image
        or not _OCI_IMAGE_REFERENCE.fullmatch(image)
    ):
        raise RunnerError("image must be a safe OCI reference")


def _image_digest_from_json(value: object, image: str) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"repodigests", "repo_digests"} and isinstance(item, list):
                for digest in item:
                    if (
                        isinstance(digest, str)
                        and _RESOLVED_IMAGE_REFERENCE.fullmatch(digest)
                    ):
                        return digest
        for key, item in value.items():
            if (
                key.lower() in {"digest", "contentdigest"}
                and isinstance(item, str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", item)
            ):
                resolved = f"{_repository_without_tag(image)}@{item}"
                return (
                    resolved
                    if _RESOLVED_IMAGE_REFERENCE.fullmatch(resolved)
                    else None
                )
        for item in value.values():
            if digest := _image_digest_from_json(item, image):
                return digest
    elif isinstance(value, list):
        for item in value:
            if digest := _image_digest_from_json(item, image):
                return digest
    elif isinstance(value, str) and _RESOLVED_IMAGE_REFERENCE.fullmatch(value):
        return value
    return None


def resolve_image_identity(runner: str, image: str) -> str:
    """Resolve a mutable image tag to the pulled content digest."""

    _validate_image_reference(image)
    command = [runner, "image", "inspect", image]
    exact_digest = bool(_RESOLVED_IMAGE_REFERENCE.fullmatch(image))
    inspected = (
        run(
            command,
            check=False,
            timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
        )
        if exact_digest
        else None
    )
    if inspected is not None and inspected.returncode == 0:
        return image

    pulled = run([runner, "image", "pull", image], check=False)
    if pulled.returncode != 0:
        detail = (pulled.stderr or pulled.stdout).strip()
        raise RunnerError(f"failed to pull image {image}: {detail}")
    inspected = run(
        command,
        check=False,
        timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
    )
    if inspected.returncode != 0:
        detail = (inspected.stderr or inspected.stdout).strip()
        raise RunnerError(f"failed to inspect pulled image {image}: {detail}")
    if exact_digest:
        return image
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"{runner} returned invalid image metadata for {image}") from exc
    identity = _image_digest_from_json(payload, image)
    if not identity:
        raise RunnerError(f"{runner} did not report a content digest for {image}")
    if not _RESOLVED_IMAGE_REFERENCE.fullmatch(identity):
        raise RunnerError(f"{runner} returned an unsafe content digest for {image}")
    return identity


def _cache_environment(
    ecosystem: str,
    *,
    prepared_volume: bool = False,
) -> tuple[str, ...]:
    if ecosystem == "python":
        root = "/prepared" if prepared_volume else "/cache"
        return (
            f"PIP_CACHE_DIR={root}/pip",
            f"UV_CACHE_DIR={root}/uv",
            "UV_LINK_MODE=copy",
        )
    if ecosystem == "node":
        root = "/prepared" if prepared_volume else "/cache"
        return (
            f"NPM_CONFIG_CACHE={root}/npm",
            f"YARN_CACHE_FOLDER={root}/yarn",
            f"COREPACK_HOME={root}/corepack-home",
        )
    if ecosystem == "bun":
        root = "/prepared" if prepared_volume else "/cache"
        return (
            f"BUN_INSTALL_CACHE_DIR={root}/bun",
            f"NPM_CONFIG_CACHE={root}/npm",
        )
    if ecosystem == "deno":
        root = "/prepared" if prepared_volume else "/cache"
        return (f"DENO_DIR={root}/deno",)
    if ecosystem == "rust":
        return (
            "CARGO_HOME=/cache/cargo-home",
            "CARGO_TARGET_DIR=/cache/cargo-target",
        )
    if ecosystem == "go":
        return (
            "GOMODCACHE=/cache/go-mod",
            "GOCACHE=/cache/go-build",
        )
    return ()


def _workspace_directory(step: CheckStep) -> str:
    if step.working_directory == ".":
        return "/workspace"
    return f"/workspace/{step.working_directory}"


def _prepare_marker_paths(
    step: CheckStep,
    *,
    effective_cache: Path | None,
    effective_volume: str | None,
    support_volume: str | None,
) -> tuple[str, ...]:
    if effective_volume and step.ecosystem == "python":
        return (f"/prepared/{_PREPARE_MARKER}",)
    if effective_volume and step.ecosystem == "deno":
        return (f"/prepared/{_PREPARE_MARKER}",)
    if effective_volume and step.ecosystem in {"node", "bun"}:
        markers = [
            f"{_workspace_directory(step)}/node_modules/{_PREPARE_MARKER}"
        ]
        if support_volume:
            markers.append(f"/prepared/{_PREPARE_MARKER}")
        return tuple(markers)
    if effective_cache and step.ecosystem in {"deno", "rust", "go"}:
        return (f"/cache/{_PREPARE_MARKER}",)
    return ()


def _cached_prepare_command(
    command: str,
    marker_paths: tuple[str, ...],
) -> str:
    if not marker_paths:
        return command
    checks = " && ".join(
        f"test -f {shlex.quote(path)}" for path in marker_paths
    )
    touches = " && ".join(
        f"touch {shlex.quote(path)}" for path in marker_paths
    )
    message = shlex.quote(_PREPARE_CACHE_HIT)
    return (
        f"if {checks}; then printf '%s\\n' {message}; "
        f"else ({command}) && {touches}; fi"
    )


def _ensure_prepared_volume(runner: str, name: str) -> None:
    inspected = run(
        [runner, "volume", "inspect", name],
        check=False,
        timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
    )
    if inspected.returncode == 0:
        record_prepared_volume(runner, name)
        return
    command = [runner, "volume", "create"]
    if runner in {"docker", "podman"}:
        command.extend(
            (
                "--label",
                "gh-freshclone.managed=true",
                "--label",
                "gh-freshclone.kind=prepared",
                "--label",
                f"gh-freshclone.cache-id={cache_namespace()}",
            )
        )
    command.append(name)
    created = run(
        command,
        check=False,
        timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()
        raise RunnerError(f"failed to create prepared cache volume {name}: {detail}")
    record_prepared_volume(runner, name)


def build_runner_command(
    runner: str,
    step: CheckStep,
    workspace: Path,
    *,
    cpus: float,
    memory: str,
    container_name: str | None = None,
    cache_dir: Path | None = None,
    prepared_volume: str | None = None,
    support_volume: str | None = None,
    image_identity: str | None = None,
    command_text: str | None = None,
    network_enabled: bool = True,
) -> list[str]:
    """Build a no-secret, resource-limited OCI invocation."""

    validate_runner_limits(runner, cpus, memory)
    _validate_image_reference(step.image)
    selected_image = image_identity or step.image
    _validate_image_reference(selected_image)
    workspace = workspace.resolve()
    resolved_cache = (
        cache_dir.resolve()
        if (
            cache_dir
            and not (
                step.ecosystem in {"python", "node", "bun", "deno"}
                and prepared_volume
            )
        )
        else None
    )
    working_directory = _workspace_directory(step)
    quoted_working_directory = shlex.quote(working_directory)
    prepared_setup = ""
    if prepared_volume and step.ecosystem == "python":
        prepared_setup = (
            "&& mkdir -p /prepared/venv /prepared/tox "
            f"&& rm -rf {quoted_working_directory}/.venv "
            f"{quoted_working_directory}/.tox "
            f"&& ln -s /prepared/venv {quoted_working_directory}/.venv "
            f"&& ln -s /prepared/tox {quoted_working_directory}/.tox "
        )
    elif prepared_volume and step.ecosystem == "deno":
        prepared_setup = (
            "&& mkdir -p /prepared/vendor /prepared/node_modules "
            f"&& rm -rf {quoted_working_directory}/vendor "
            f"{quoted_working_directory}/node_modules "
            f"&& ln -s /prepared/vendor {quoted_working_directory}/vendor "
            f"&& ln -s /prepared/node_modules "
            f"{quoted_working_directory}/node_modules "
        )
    shell_command = (
        'mkdir -p "$HOME" /workspace /cache '
        "&& cp -R /input/. /workspace/ "
        + prepared_setup
        + f"&& cd {quoted_working_directory} && "
        + (step.command if command_text is None else command_text)
    )

    if runner in {"docker", "podman"}:
        command = [
            runner,
            "run",
            "--rm",
            "--label=gh-freshclone.managed=true",
            f"--label=gh-freshclone.cache-id={cache_namespace()}",
            "--label=gh-freshclone.kind=execution",
            "--cap-drop=ALL",
            "--cap-add=CHOWN",
            "--cap-add=FOWNER",
            "--security-opt=no-new-privileges",
            "--pids-limit=512",
            f"--cpus={cpus:g}",
            f"--memory={memory}",
            "--env=CI=1",
            "--env=HOME=/tmp/freshclone-home",
            "--tmpfs=/tmp:rw,exec,nosuid,nodev,size=2g",
            f"--mount=type=bind,source={workspace},target=/input,readonly",
            "--workdir=/",
            "--entrypoint=sh",
        ]
        if not network_enabled:
            command.append("--network=none")
        command.extend(
            f"--env={value}"
            for value in _cache_environment(
                step.ecosystem,
                prepared_volume=bool(prepared_volume),
            )
        )
        if resolved_cache:
            command.append(
                f"--mount=type=bind,source={resolved_cache},target=/cache"
            )
        if prepared_volume and step.ecosystem in {"python", "deno"}:
            command.append(
                f"--mount=type=volume,source={prepared_volume},target=/prepared"
            )
        elif prepared_volume and step.ecosystem in {"node", "bun"}:
            command.append(
                f"--volume={prepared_volume}:"
                f"{working_directory}/node_modules"
            )
        if support_volume:
            command.append(
                f"--mount=type=volume,source={support_volume},target=/prepared"
            )
        if container_name:
            command.append(f"--name={container_name}")
        # A login shell replaces image-defined PATH entries such as
        # /usr/local/go/bin and /usr/local/cargo/bin. The image entrypoint is
        # already bypassed; keep its declared environment with a plain shell.
        return command + [selected_image, "-c", shell_command]
    if runner == "container":
        command = [
            "container",
            "run",
            "--rm",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cpus",
            f"{cpus:g}",
            "--memory",
            memory,
            "--env",
            "CI=1",
            "--env",
            "HOME=/tmp/freshclone-home",
            "--tmpfs",
            _CONTAINER_TMP,
            "--mount",
            f"type=bind,source={workspace},target=/input,readonly",
            "--entrypoint",
            "sh",
        ]
        if not network_enabled:
            command.extend(("--network", "none"))
        for value in _cache_environment(
            step.ecosystem,
            prepared_volume=bool(prepared_volume),
        ):
            command.extend(("--env", value))
        if resolved_cache:
            command.extend(("--volume", f"{resolved_cache}:/cache"))
        if prepared_volume and step.ecosystem in {"python", "deno"}:
            command.extend(("--volume", f"{prepared_volume}:/prepared"))
        elif prepared_volume and step.ecosystem in {"node", "bun"}:
            command.extend(
                (
                    "--volume",
                    f"{prepared_volume}:{working_directory}/node_modules",
                )
            )
        if support_volume:
            command.extend(("--volume", f"{support_volume}:/prepared"))
        if container_name:
            command.extend(("--name", container_name))
        return command + [selected_image, "-c", shell_command]
    raise RunnerError(f"unknown runner: {runner}")


def _stop_execution(
    runner: str,
    container_name: str,
    proc: subprocess.Popen[str] | None,
) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    if runner in {"docker", "podman"}:
        run(
            [runner, "rm", "--force", container_name],
            check=False,
            timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
        )
    elif runner == "container":
        run(
            ["container", "stop", container_name],
            check=False,
            timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
        )
        run(
            ["container", "delete", container_name],
            check=False,
            timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
        )


def _probe_missing_executables(
    runner: str,
    image_identity: str,
    candidates: set[str],
) -> tuple[str, ...]:
    """Verify failure-related executable candidates against the exact image."""

    if not candidates:
        return ()
    names = sorted(candidates)
    script = (
        "for name in "
        + " ".join(names)
        + '; do command -v "$name" >/dev/null 2>&1 || printf "%s\\n" "$name"; done'
    )
    if runner in {"docker", "podman"}:
        command = [
            runner,
            "run",
            "--rm",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--entrypoint=sh",
            image_identity,
            "-c",
            script,
        ]
    elif runner == "container":
        command = [
            "container",
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--entrypoint",
            "sh",
            image_identity,
            "-c",
            script,
        ]
    else:
        return ()
    probed = run(
        command,
        check=False,
        timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
    )
    if probed.returncode != 0:
        return ()
    reported = set(probed.stdout.splitlines())
    return tuple(name for name in names if name in reported)


def _execute_phase(
    *,
    runner: str,
    command: list[str],
    container_name: str,
    log_path: Path,
    phase: str,
    echo: bool,
) -> _PhaseExecution:
    started = time.monotonic()
    tail: list[str] = []
    diagnostics: list[str] = []
    failure_candidates: set[str] = set()
    proc: subprocess.Popen[str] | None = None
    lines_since_flush = 0
    last_flush = time.monotonic()
    header = f"=== gh-freshclone {phase} phase ===\n"
    truncation_marker = (
        f"\n=== gh-freshclone output truncated at {MAX_LOG_BYTES} bytes ===\n"
    )
    written_bytes = log_path.stat().st_size if log_path.exists() else 0
    log_truncated = written_bytes >= MAX_LOG_BYTES
    previous_sigterm = _install_cleanup_sigterm()
    try:
        with log_path.open(
            "a",
            encoding="utf-8",
            errors="replace",
            newline="\n",
        ) as log:
            header_bytes = header.encode()
            if not log_truncated and written_bytes + len(header_bytes) <= MAX_LOG_BYTES:
                log.write(header)
                written_bytes += len(header_bytes)
            log.flush()
            if echo:
                print(_console_safe(header, sys.stdout.encoding), end="")
            # Runner commands are fixed argv structures; shell execution is unused.
            proc = subprocess.Popen(  # nosec B603
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.stdout is None:
                raise RunnerError(f"{runner} output pipe was not created")
            while line := proc.stdout.readline(8192):
                encoded = line.encode("utf-8", errors="replace")
                if not log_truncated:
                    remaining = MAX_LOG_BYTES - written_bytes
                    marker_bytes = truncation_marker.encode()
                    if len(encoded) <= remaining:
                        log.write(line)
                        written_bytes += len(encoded)
                        lines_since_flush += 1
                    else:
                        content_bytes = max(0, remaining - len(marker_bytes))
                        if content_bytes:
                            log.write(
                                encoded[:content_bytes].decode(
                                    "utf-8",
                                    errors="ignore",
                                )
                            )
                        if remaining >= len(marker_bytes):
                            log.write(truncation_marker)
                        written_bytes = MAX_LOG_BYTES
                        log_truncated = True
                        lines_since_flush += 1
                now = time.monotonic()
                if lines_since_flush >= 128 or now - last_flush >= 1:
                    log.flush()
                    lines_since_flush = 0
                    last_flush = now
                sampled_line = line if len(line) <= 16_000 else line[:8_000] + line[-8_000:]
                tail.append(sampled_line.rstrip()[-2_000:])
                if len(tail) > 20:
                    tail.pop(0)
                lowered = line.lower()
                failure_candidates.update(
                    failure_executable_candidates(sampled_line)
                )
                if any(
                    marker in lowered
                    for marker in (
                        "command not found",
                        "executable file not found",
                        "no such file or directory",
                        "permissionerror",
                        "fatalerror",
                        "executable `",
                        "network is unreachable",
                        "temporary failure in name resolution",
                        "could not resolve host",
                        "name or service not known",
                        "failed to lookup address information",
                        "dns error:",
                        "enetunreach",
                        "eai_again",
                        "getaddrinfo",
                    )
                ):
                    diagnostics.append(sampled_line.rstrip()[-2_000:])
                    if len(diagnostics) > 20:
                        diagnostics.pop(0)
                if echo:
                    print(_console_safe(line, sys.stdout.encoding), end="")
            returncode = proc.wait()
    except KeyboardInterrupt:
        _stop_execution(runner, container_name, proc)
        raise
    except OSError as exc:
        _stop_execution(runner, container_name, proc)
        raise RunnerError(f"failed to start {runner}: {exc}") from exc
    except BaseException:
        _stop_execution(runner, container_name, proc)
        raise
    finally:
        _restore_sigterm(previous_sigterm)

    if returncode != 0:
        _stop_execution(runner, container_name, proc)

    detail_lines = (
        tail
        + (["--- diagnostic evidence ---"] if diagnostics else [])
        + diagnostics
    )
    return _PhaseExecution(
        returncode=returncode,
        duration_seconds=time.monotonic() - started,
        detail="\n".join(detail_lines)[-4000:],
        failure_candidates=frozenset(failure_candidates),
    )


def _run_step_phases(
    runner: str,
    step: CheckStep,
    workspace: Path,
    log_path: Path,
    *,
    cpus: float,
    memory: str,
    echo: bool,
    effective_cache: Path | None,
    effective_volume: str | None,
    support_volume: str | None,
    image_identity: str,
    cache_key: str,
    prepare_marker_paths: tuple[str, ...],
    reset_log: bool = True,
) -> RunnerExecution:
    execution_id = uuid.uuid4().hex[:12]
    version = runner_version(runner)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if reset_log:
        log_path.write_text("", encoding="utf-8")
    total_started = time.monotonic()
    prepare_duration = 0.0
    prepare_cache_hit: bool | None = None
    phase = "test"
    result: _PhaseExecution | None = None
    if step.prepare_command:
        phase = "prepare"
        prepare = _execute_phase(
            runner=runner,
            command=build_runner_command(
                runner,
                step,
                workspace,
                cpus=cpus,
                memory=memory,
                container_name=f"gh-freshclone-{execution_id}-prepare",
                cache_dir=effective_cache,
                prepared_volume=effective_volume,
                support_volume=support_volume,
                image_identity=image_identity,
                command_text=_cached_prepare_command(
                    step.prepare_command,
                    prepare_marker_paths,
                ),
                network_enabled=True,
            ),
            container_name=f"gh-freshclone-{execution_id}-prepare",
            log_path=log_path,
            phase="prepare (network enabled)",
            echo=echo,
        )
        prepare_duration = prepare.duration_seconds
        prepare_cache_hit = _PREPARE_CACHE_HIT in prepare.detail
        if prepare.returncode != 0:
            result = prepare
        else:
            phase = "test"
    if phase == "test":
        result = _execute_phase(
            runner=runner,
            command=build_runner_command(
                runner,
                step,
                workspace,
                cpus=cpus,
                memory=memory,
                container_name=f"gh-freshclone-{execution_id}-test",
                cache_dir=effective_cache,
                prepared_volume=effective_volume,
                support_volume=support_volume,
                image_identity=image_identity,
                command_text=step.command,
                network_enabled=step.test_network == "enabled",
            ),
            container_name=f"gh-freshclone-{execution_id}-test",
            log_path=log_path,
            phase=f"test (network {step.test_network})",
            echo=echo,
        )
    if result is None:
        raise RunnerError("runner phase selection produced no execution")
    observed_missing = (
        _probe_missing_executables(
            runner,
            image_identity,
            set(result.failure_candidates),
        )
        if result.returncode != 0
        else ()
    )
    return RunnerExecution(
        returncode=result.returncode,
        duration_seconds=time.monotonic() - total_started,
        detail=result.detail,
        image_identity=image_identity,
        runner_version=version,
        observed_missing_executables=observed_missing,
        dependency_cache=cache_key[:24],
        prepared_volume=effective_volume or "",
        prepare_duration_seconds=prepare_duration,
        test_network=step.test_network,
        failed_phase=phase if result.returncode != 0 else None,
        prepare_cache_hit=prepare_cache_hit,
    )


def _discard_failed_preparation(
    runner: str,
    *,
    cache_path: Path | None,
    volume_names: list[str],
) -> tuple[bool, str | None]:
    resources = 0
    failures: list[str] = []
    if cache_path is not None:
        resources += 1
        try:
            removed, error = discard_dependency_cache(cache_path)
        except OSError as exc:
            removed, error = False, str(exc)
        if not removed:
            failures.append(error or f"host cache {cache_path.name}")
    for name in volume_names:
        resources += 1
        try:
            removed, error = discard_prepared_volume(runner, name)
        except OSError as exc:
            removed, error = False, str(exc)
        if not removed:
            failures.append(error or f"prepared volume {name}")
    if resources == 0:
        return False, None
    if failures:
        return (
            False,
            (
                "gh-freshclone: dependency preparation failed; "
                f"could not discard {len(failures)} of "
                f"{resources} scoped cache resources"
            ),
        )
    return (
        True,
        (
            "gh-freshclone: dependency preparation failed; "
            f"discarded {resources} scoped cache resources before one clean retry"
        ),
    )


def _append_execution_message(
    execution: RunnerExecution,
    log_path: Path,
    message: str,
) -> RunnerExecution:
    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write(f"\n=== {message} ===\n")
    except OSError:
        pass
    return replace(
        execution,
        detail=f"{execution.detail}\n{message}"[-4000:],
    )


def run_step(
    runner: str,
    step: CheckStep,
    workspace: Path,
    log_path: Path,
    *,
    cpus: float,
    memory: str,
    echo: bool = True,
    cache_dir: Path | None = None,
    prepared_volume: str | None = None,
) -> RunnerExecution:
    image_identity = resolve_image_identity(runner, step.image)
    cache_key = execution_cache_key(step, image_identity)
    image_key = hashlib.sha256(image_identity.encode()).hexdigest()[:12]
    stateful_ecosystems = {"python", "node", "bun", "deno"}
    effective_volume = (
        f"{prepared_volume}-i{image_key}"
        if (
            prepared_volume
            and step.prepare_command
            and step.ecosystem in stateful_ecosystems
        )
        else None
    )
    support_volume = (
        f"{effective_volume}-support"
        if effective_volume and step.ecosystem in {"node", "bun"}
        else None
    )
    effective_cache = (
        cache_dir.with_name(cache_key[:24])
        if cache_dir and not effective_volume
        else None
    )
    path_resources = [effective_cache] if effective_cache else []
    volume_resources = [
        name for name in (effective_volume, support_volume) if name
    ]
    prepare_marker_paths = _prepare_marker_paths(
        step,
        effective_cache=effective_cache,
        effective_volume=effective_volume,
        support_volume=support_volume,
    )
    with ExitStack() as locks:
        for path in sorted(path_resources, key=lambda item: str(item.absolute())):
            locks.enter_context(cache_path_lock(path))
        for name in sorted(volume_resources):
            locks.enter_context(cache_volume_lock(runner, name))
        if effective_cache:
            touch_dependency_cache(effective_cache)
        if effective_volume:
            _ensure_prepared_volume(runner, effective_volume)
        if support_volume:
            _ensure_prepared_volume(runner, support_volume)
        execution = _run_step_phases(
            runner,
            step,
            workspace,
            log_path,
            cpus=cpus,
            memory=memory,
            echo=echo,
            effective_cache=effective_cache,
            effective_volume=effective_volume,
            support_volume=support_volume,
            image_identity=image_identity,
            cache_key=cache_key,
            prepare_marker_paths=prepare_marker_paths,
        )
        if execution.returncode != 0 and execution.failed_phase == "prepare":
            discarded, message = _discard_failed_preparation(
                runner,
                cache_path=effective_cache,
                volume_names=volume_resources,
            )
            if message:
                execution = _append_execution_message(
                    execution,
                    log_path,
                    message,
                )
            if discarded:
                try:
                    if effective_cache:
                        touch_dependency_cache(effective_cache)
                    if effective_volume:
                        _ensure_prepared_volume(runner, effective_volume)
                    if support_volume:
                        _ensure_prepared_volume(runner, support_volume)
                except (OSError, RunnerError) as exc:
                    return _append_execution_message(
                        execution,
                        log_path,
                        "gh-freshclone: could not recreate the clean scoped "
                        f"dependency cache: {exc}",
                    )
                first_execution = execution
                execution = _run_step_phases(
                    runner,
                    step,
                    workspace,
                    log_path,
                    cpus=cpus,
                    memory=memory,
                    echo=echo,
                    effective_cache=effective_cache,
                    effective_volume=effective_volume,
                    support_volume=support_volume,
                    image_identity=image_identity,
                    cache_key=cache_key,
                    prepare_marker_paths=prepare_marker_paths,
                    reset_log=False,
                )
                execution = replace(
                    execution,
                    duration_seconds=(
                        first_execution.duration_seconds
                        + execution.duration_seconds
                    ),
                    prepare_duration_seconds=(
                        first_execution.prepare_duration_seconds
                        + execution.prepare_duration_seconds
                    ),
                )
                execution = _append_execution_message(
                    execution,
                    log_path,
                    "gh-freshclone: retried preparation once from a clean "
                    "scoped cache",
                )
                if (
                    execution.returncode != 0
                    and execution.failed_phase == "prepare"
                ):
                    _, final_message = _discard_failed_preparation(
                        runner,
                        cache_path=effective_cache,
                        volume_names=volume_resources,
                    )
                    if final_message:
                        execution = _append_execution_message(
                            execution,
                            log_path,
                            final_message.replace(
                                "before one clean retry",
                                "after the clean retry",
                            ),
                        )
        return execution


def classify_exit(returncode: int, detail: str) -> str:
    return diagnose_failure(returncode, detail)[0]
