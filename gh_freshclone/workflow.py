from __future__ import annotations

import platform
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .model import BaselinePlan, Receipt, Repository, ResourceLimits, StepResult
from .receipts import (
    dependency_cache_path,
    prepared_volume_name,
    read_indexed_pass_receipt,
    read_pass_receipt,
    receipt_path,
    write_receipt,
)
from .runner_policy import (
    preferred_runner,
    select_runner,
    validate_runner_limits,
)

if TYPE_CHECKING:
    from .runners import RunnerExecution


class WorkflowError(RuntimeError):
    pass


def resolve_repository(target: str, ref: str | None = None) -> Repository:
    from .github import resolve_repository as implementation

    return implementation(target, ref)


def materialize(repository: Repository, destination: Path) -> None:
    from .github import materialize as implementation

    implementation(repository, destination)


def run_step(*args: Any, **kwargs: Any) -> RunnerExecution:
    from .runners import run_step as implementation

    return implementation(*args, **kwargs)


def create_plan(
    target: str,
    ref: str | None = None,
    *,
    profile: str = "quick",
) -> BaselinePlan:
    from .detect import detect_plan

    repository = resolve_repository(target, ref)
    with tempfile.TemporaryDirectory(
        prefix="gh-freshclone-plan-",
        ignore_cleanup_errors=True,
    ) as temporary:
        checkout = Path(temporary) / "source"
        materialize(repository, checkout)
        return detect_plan(repository, checkout, profile)


def check_repository(
    target: str,
    *,
    ref: str | None = None,
    runner: str = "auto",
    cpus: float = 4,
    memory: str = "8g",
    use_cache: bool = True,
    echo: bool = True,
    profile: str = "quick",
) -> tuple[Receipt | dict, Path, bool]:
    resource_limits = ResourceLimits(cpus=cpus, memory=memory)
    selected_runner = preferred_runner(runner)
    validate_runner_limits(
        selected_runner,
        resource_limits.cpus,
        resource_limits.memory,
    )
    repository = resolve_repository(target, ref)
    if use_cache and (
        indexed := read_indexed_pass_receipt(
            repository,
            profile,
            selected_runner,
            resource_limits,
        )
    ):
        receipt, path = indexed
        return receipt, path, True

    ready_runner = select_runner(runner)
    validate_runner_limits(
        ready_runner,
        resource_limits.cpus,
        resource_limits.memory,
    )
    if ready_runner != selected_runner:
        selected_runner = ready_runner
        if use_cache and (
            indexed := read_indexed_pass_receipt(
                repository,
                profile,
                selected_runner,
                resource_limits,
            )
        ):
            receipt, path = indexed
            return receipt, path, True
    else:
        selected_runner = ready_runner

    from .cache import cache_lock

    lock_key = (
        f"{repository.display_name}\0{repository.commit_sha}\0"
        f"{profile}\0{selected_runner}\0"
        f"{resource_limits.cpus:g}\0{resource_limits.memory}"
    )
    with cache_lock(lock_key):
        if use_cache and (
            indexed := read_indexed_pass_receipt(
                repository,
                profile,
                selected_runner,
                resource_limits,
            )
        ):
            receipt, path = indexed
            return receipt, path, True
        return _check_resolved_repository(
            repository,
            selected_runner,
            resource_limits,
            use_cache=use_cache,
            echo=echo,
            profile=profile,
        )


def _check_resolved_repository(
    repository: Repository,
    selected_runner: str,
    resource_limits: ResourceLimits,
    *,
    use_cache: bool,
    echo: bool,
    profile: str,
) -> tuple[Receipt | dict, Path, bool]:
    from .cache import cache_path_lock, maybe_prune_cache
    from .detect import detect_plan
    from .diagnostics import diagnose_failure

    with tempfile.TemporaryDirectory(
        prefix="gh-freshclone-check-",
        ignore_cleanup_errors=True,
    ) as temporary, ExitStack() as evidence_locks:
        temporary_root = Path(temporary)
        checkout = temporary_root / "source"
        materialize(repository, checkout)
        plan = detect_plan(repository, checkout, profile)
        if not plan.steps:
            raise WorkflowError("no executable baseline was detected")

        destination = receipt_path(plan, selected_runner, resource_limits)
        if use_cache and (
            cached := read_pass_receipt(
                destination,
                plan=plan,
                runner=selected_runner,
                resource_limits=resource_limits,
            )
        ):
            return cached, destination, True

        evidence_locks.enter_context(cache_path_lock(destination))
        results: list[StepResult] = []
        last_execution: RunnerExecution | None = None
        for index, step in enumerate(plan.steps, start=1):
            # Every runner mounts the canonical fresh checkout read-only, then
            # copies it into its own ephemeral container. A second host copy adds
            # repository-sized I/O without improving isolation.
            workspace = checkout
            log_path = destination.with_name(
                f"{destination.stem}-{index}-{step.ecosystem}.log"
            )
            evidence_locks.enter_context(cache_path_lock(log_path))
            cache_path = dependency_cache_path(
                repository,
                step,
                selected_runner,
            )
            volume_name = prepared_volume_name(
                repository,
                step,
                profile,
            )
            execution = run_step(
                selected_runner,
                step,
                workspace,
                log_path,
                cpus=resource_limits.cpus,
                memory=resource_limits.memory,
                echo=echo,
                cache_dir=cache_path,
                prepared_volume=volume_name,
            )
            last_execution = execution
            status, diagnostics = diagnose_failure(
                execution.returncode,
                execution.detail,
                observed_missing_executables=execution.observed_missing_executables,
                test_network=(
                    execution.test_network
                    if execution.failed_phase == "test"
                    else "enabled"
                ),
                failed_phase=execution.failed_phase,
            )
            results.append(
                StepResult(
                    ecosystem=step.ecosystem,
                    image=step.image,
                    image_identity=execution.image_identity or step.image,
                    command=step.command,
                    status=status,
                    exit_code=execution.returncode,
                    duration_seconds=round(execution.duration_seconds, 3),
                    log_path=str(log_path),
                    detail=execution.detail,
                    diagnostics=diagnostics,
                    dependency_cache=execution.dependency_cache or None,
                    prepare_duration_seconds=round(
                        execution.prepare_duration_seconds,
                        3,
                    ),
                    test_network=execution.test_network,
                    failed_phase=execution.failed_phase,
                    prepared_volume=execution.prepared_volume or None,
                    prepare_cache_hit=execution.prepare_cache_hit,
                )
            )
            if status != "pass":
                break

        overall = "pass" if len(results) == len(plan.steps) and all(
            result.status == "pass" for result in results
        ) else results[-1].status
        if last_execution is None:
            raise WorkflowError("detected plan produced no runner execution")
        observed_runner_version = last_execution.runner_version
        receipt = Receipt(
            created_at=datetime.now(UTC).isoformat(),
            status=overall,
            runner=selected_runner,
            runner_version=observed_runner_version,
            host_platform=platform.platform(),
            plan=plan,
            resource_limits=resource_limits,
            results=tuple(results),
        )
        write_receipt(destination, receipt)
        evidence_locks.close()
        maybe_prune_cache()
        return receipt, destination, False
