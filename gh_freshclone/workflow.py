from __future__ import annotations

import platform
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import TEST_NETWORK_POLICIES
from .model import (
    BaselinePlan,
    Receipt,
    Repository,
    ResourceLimits,
    StepResult,
    normalize_component,
)
from .receipts import (
    dependency_cache_path,
    prepared_volume_name,
    read_indexed_pass_receipt,
    read_pass_receipt,
    receipt_path,
    source_repository_cache_path,
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


_REPOSITORY_NETWORK_OVERRIDDEN_WARNING = (
    "Repository configuration requested outbound test network; "
    "operator policy kept every test step offline. Use "
    "`--test-network enabled` to opt in."
)
_OPERATOR_NETWORK_ENABLED_WARNING = (
    "Operator policy enabled outbound network for every test step."
)


def resolve_repository(target: str, ref: str | None = None) -> Repository:
    from .github import resolve_repository as implementation

    return implementation(target, ref)


def materialize(repository: Repository, destination: Path) -> None:
    from .github import materialize as implementation

    implementation(repository, destination)


def materialize_plan_inputs(
    repository: Repository,
    destination: Path,
    component: str = ".",
    *,
    source_cache: Path | None = None,
) -> bool:
    from .github import materialize_plan_inputs as implementation

    return implementation(
        repository,
        destination,
        component,
        source_cache=source_cache,
    )


def complete_materialization(
    repository: Repository,
    destination: Path,
    component: str = ".",
    *,
    allow_lazy_fetch: bool = True,
) -> None:
    from .github import complete_materialization as implementation

    implementation(
        repository,
        destination,
        component,
        allow_lazy_fetch=allow_lazy_fetch,
    )


def store_source_cache(
    repository: Repository,
    workspace: Path,
    cache: Path,
    component: str = ".",
) -> bool:
    from .github import store_source_cache as implementation

    return implementation(repository, workspace, cache, component)


def run_step(*args: Any, **kwargs: Any) -> RunnerExecution:
    from .runners import run_step as implementation

    return implementation(*args, **kwargs)


def create_plan(
    target: str,
    ref: str | None = None,
    *,
    profile: str = "quick",
    test_network: str = "none",
    component: str = ".",
) -> BaselinePlan:
    from .detect import detect_plan

    component = normalize_component(component)
    repository = resolve_repository(target, ref)
    with tempfile.TemporaryDirectory(
        prefix="gh-freshclone-plan-",
        ignore_cleanup_errors=True,
    ) as temporary:
        checkout = Path(temporary) / "source"
        materialize_plan_inputs(repository, checkout, component)
        return _apply_test_network_policy(
            detect_plan(
                repository,
                checkout,
                profile,
                component=component,
            ),
            test_network,
        )


def _apply_test_network_policy(
    plan: BaselinePlan,
    test_network: str,
) -> BaselinePlan:
    if test_network not in TEST_NETWORK_POLICIES:
        choices = ", ".join(TEST_NETWORK_POLICIES)
        raise ValueError(f"test_network must be one of: {choices}")
    requested = {step.test_network for step in plan.steps}
    warnings = [
        warning
        for warning in plan.warnings
        if warning
        not in {
            _REPOSITORY_NETWORK_OVERRIDDEN_WARNING,
            _OPERATOR_NETWORK_ENABLED_WARNING,
        }
    ]
    if plan.steps and test_network == "none" and "enabled" in requested:
        warnings.append(_REPOSITORY_NETWORK_OVERRIDDEN_WARNING)
    elif plan.steps and test_network == "enabled" and requested != {"enabled"}:
        warnings.append(_OPERATOR_NETWORK_ENABLED_WARNING)
    return replace(
        plan,
        steps=tuple(
            replace(step, test_network=test_network)
            for step in plan.steps
        ),
        warnings=tuple(warnings),
    )


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
    test_network: str = "none",
    component: str = ".",
) -> tuple[Receipt | dict, Path, bool]:
    if test_network not in TEST_NETWORK_POLICIES:
        choices = ", ".join(TEST_NETWORK_POLICIES)
        raise ValueError(f"test_network must be one of: {choices}")
    component = normalize_component(component)
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
            test_network,
            component,
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
                test_network,
                component,
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
        f"{resource_limits.cpus:g}\0{resource_limits.memory}\0{test_network}\0"
        f"{component}"
    )
    with cache_lock(lock_key):
        if use_cache and (
            indexed := read_indexed_pass_receipt(
                repository,
                profile,
                selected_runner,
                resource_limits,
                test_network,
                component,
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
            test_network=test_network,
            component=component,
        )


def _check_resolved_repository(
    repository: Repository,
    selected_runner: str,
    resource_limits: ResourceLimits,
    *,
    use_cache: bool,
    echo: bool,
    profile: str,
    test_network: str,
    component: str,
) -> tuple[Receipt | dict, Path, bool]:
    from .cache import (
        CacheSpaceError,
        cache_path_lock,
        discard_dependency_cache,
        ensure_storage_reserve,
        maybe_prune_cache,
        touch_dependency_cache,
    )
    from .detect import detect_plan
    from .diagnostics import diagnose_failure
    from .github import SourceCacheError
    from .workspace_archive import WorkspaceArchiveError, create_workspace_archive

    try:
        ensure_storage_reserve()
    except CacheSpaceError as exc:
        raise WorkflowError(str(exc)) from exc

    with tempfile.TemporaryDirectory(
        prefix="gh-freshclone-check-",
        ignore_cleanup_errors=True,
    ) as temporary, ExitStack() as evidence_locks, ExitStack() as source_locks:
        temporary_root = Path(temporary)
        checkout = temporary_root / "source"
        source_cache = (
            source_repository_cache_path(repository, component)
            if repository.github_repository and not repository.local_path
            else None
        )
        source_cache_hit = False
        host_cache_changed = False
        if source_cache is not None:
            source_locks.enter_context(cache_path_lock(source_cache))
            try:
                source_cache_hit = materialize_plan_inputs(
                    repository,
                    checkout,
                    component,
                    source_cache=source_cache,
                )
                if source_cache_hit:
                    touch_dependency_cache(source_cache)
            except SourceCacheError as exc:
                removed, removal_error = discard_dependency_cache(source_cache)
                if source_cache.exists() or (not removed and removal_error):
                    detail = removal_error or str(exc)
                    raise WorkflowError(
                        f"invalid source cache could not be discarded: {detail}"
                    ) from exc
                shutil.rmtree(checkout, ignore_errors=True)
                materialize_plan_inputs(repository, checkout, component)
        else:
            materialize_plan_inputs(repository, checkout, component)
        plan = _apply_test_network_policy(
            detect_plan(
                repository,
                checkout,
                profile,
                component=component,
            ),
            test_network,
        )
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
        complete_materialization(
            repository,
            checkout,
            component,
            allow_lazy_fetch=not source_cache_hit,
        )
        workspace_archive = temporary_root / "workspace.tar"
        try:
            create_workspace_archive(
                checkout,
                workspace_archive,
                repository.commit_sha,
                component=component,
            )
            if source_cache is not None and not source_cache_hit:
                try:
                    source_cache_changed = store_source_cache(
                        repository,
                        checkout,
                        source_cache,
                        component,
                    )
                    if source_cache_changed:
                        touch_dependency_cache(source_cache)
                        host_cache_changed = True
                except SourceCacheError as exc:
                    if echo:
                        print(
                            "gh-freshclone: immutable source cache unavailable; "
                            f"continuing without reuse ({exc})"
                        )
        except WorkspaceArchiveError as exc:
            workspace_archive = None
            if echo:
                print(
                    "gh-freshclone: workspace archive unavailable; "
                    f"using bind copy ({exc})"
                )
        source_locks.close()
        results: list[StepResult] = []
        last_execution: RunnerExecution | None = None
        prepared_volumes_changed = False
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
                workspace_archive=workspace_archive,
            )
            last_execution = execution
            prepared_volumes_changed = prepared_volumes_changed or bool(
                execution.prepared_volume
                and execution.prepare_cache_hit is False
            )
            host_cache_changed = host_cache_changed or bool(
                execution.dependency_cache
                and not execution.prepared_volume
                and execution.prepare_cache_hit is False
            )
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
            source_cache_hit=source_cache_hit,
            source_validation=(
                "git-fsck-full-strict"
                if source_cache_hit
                else (
                    "local-git-clone"
                    if repository.local_path
                    else "fresh-git-fetch"
                )
            ),
        )
        write_receipt(destination, receipt)
        evidence_locks.close()
        maybe_prune_cache(
            prepared_volumes_changed=prepared_volumes_changed,
            host_cache_changed=host_cache_changed,
            protected_path=source_cache,
        )
        return receipt, destination, False
