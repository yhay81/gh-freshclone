from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import time
from pathlib import Path

from .model import (
    EXECUTION_POLICY_VERSION,
    PLAN_VERSION,
    RECEIPT_VERSION,
    BaselinePlan,
    CheckStep,
    Receipt,
    Repository,
    ResourceLimits,
    normalize_component,
)

_EVIDENCE_TOUCH_INTERVAL_SECONDS = 60 * 60


def cache_root() -> Path:
    override = os.environ.get("GH_FRESHCLONE_CACHE")
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "gh-freshclone" if base else Path.home() / ".gh-freshclone"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "gh-freshclone"
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base) / "gh-freshclone" if base else Path.home() / ".cache" / "gh-freshclone"


def cache_namespace() -> str:
    """Identify the selected cache root without exposing its host path."""

    value = str(cache_root().resolve())
    if platform.system() == "Windows":
        value = value.casefold()
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def plan_digest(plan: BaselinePlan) -> str:
    payload = json.dumps(
        plan.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _repository_namespace(repository: Repository) -> str:
    if repository.github_repository:
        identity = f"github:{repository.github_repository.casefold()}"
    elif repository.local_path:
        local_path = str(Path(repository.local_path).absolute())
        if platform.system() == "Windows":
            local_path = local_path.casefold()
        identity = f"local:{local_path}"
    else:
        identity = f"display:{repository.display_name}"
    prefix = _safe_name(repository.display_name)[:80].strip("._-") or "repository"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def execution_context_digest(runner: str, resource_limits: ResourceLimits) -> str:
    payload = json.dumps(
        {
            "runner": runner,
            "resource_limits": resource_limits.to_dict(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(b"gh-freshclone-execution-context-v1\0" + payload).hexdigest()


def receipt_path(
    plan: BaselinePlan,
    runner: str,
    resource_limits: ResourceLimits,
) -> Path:
    name = _repository_namespace(plan.repository)
    context = execution_context_digest(runner, resource_limits)
    filename = (
        f"{plan.repository.commit_sha[:12]}-{plan_digest(plan)[:12]}-"
        f"{_safe_name(runner)}-{context[:12]}-"
        f"r{RECEIPT_VERSION}-e{EXECUTION_POLICY_VERSION}.json"
    )
    return cache_root() / "receipts" / name / filename


def dependency_cache_path(
    repository: Repository,
    step: CheckStep,
    runner: str,
) -> Path:
    """Return a repo- and lockfile-scoped cache with no cross-repository sharing."""

    name = _repository_namespace(repository)
    fingerprint = step.dependency_fingerprint or "uncacheable"
    return (
        cache_root()
        / "runner-cache"
        / _safe_name(runner)
        / name
        / step.ecosystem
        / fingerprint[:24]
    )


def _preparation_context(step: CheckStep) -> dict[str, str]:
    return {
        "ecosystem": step.ecosystem,
        "image": step.image,
        "dependency_fingerprint": step.dependency_fingerprint,
        "prepare_command": step.prepare_command,
        "working_directory": step.working_directory,
    }


def execution_cache_key(step: CheckStep, image_identity: str) -> str:
    """Bind prepared dependencies, but not test-only behavior, to image content."""

    digest = hashlib.sha256()
    digest.update(b"gh-freshclone-execution-cache-v3\0")
    digest.update(
        json.dumps(
            _preparation_context(step),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    digest.update(b"\0")
    digest.update(image_identity.encode())
    return digest.hexdigest()


def prepared_volume_name(
    repository: Repository,
    step: CheckStep,
    profile: str,
) -> str:
    """Return a deterministic OCI-managed volume for prepared dependencies."""

    repository_key = _repository_namespace(repository).rsplit("-", 1)[-1]
    profile_key = _safe_name(profile)[:8]
    ecosystem_key = _safe_name(step.ecosystem)[:8]
    fingerprint = (step.dependency_fingerprint or "uncacheable")[:12]
    step_key = hashlib.sha256(
        json.dumps(
            _preparation_context(step),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:8]
    return (
        f"ghfc-{repository_key}-{repository.commit_sha[:12]}-"
        f"c{cache_namespace()}-{profile_key}-{ecosystem_key}-s{step_key}-"
        f"{fingerprint}-"
        f"p{PLAN_VERSION}-e{EXECUTION_POLICY_VERSION}"
    )


def _index_path(
    repository: Repository,
    profile: str,
    runner: str,
    resource_limits: ResourceLimits,
    test_network: str | None,
    component: str = ".",
) -> Path:
    name = _repository_namespace(repository)
    context = execution_context_digest(runner, resource_limits)
    component = normalize_component(component)
    component_key = hashlib.sha256(component.encode()).hexdigest()[:12]
    network = (
        ""
        if test_network is None
        else f"-n{_safe_name(test_network)}"
    )
    filename = (
        f"{repository.commit_sha}-{_safe_name(profile)}-{_safe_name(runner)}"
        f"-{context[:12]}{network}-c{component_key}-p{PLAN_VERSION}-"
        f"e{EXECUTION_POLICY_VERSION}.json"
    )
    return cache_root() / "indexes" / name / filename


def read_pass_receipt(
    path: Path,
    *,
    plan: BaselinePlan | None = None,
    runner: str | None = None,
    resource_limits: ResourceLimits | None = None,
    touch: bool = True,
) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("status") != "pass":
        return None
    if (
        value.get("receipt_version") != RECEIPT_VERSION
        or value.get("execution_policy_version") != EXECUTION_POLICY_VERSION
    ):
        return None
    if plan is not None and value.get("plan") != plan.to_dict():
        return None
    if runner is not None and value.get("runner") != runner:
        return None
    if (
        resource_limits is not None
        and value.get("resource_limits") != resource_limits.to_dict()
    ):
        return None
    if touch:
        _touch_cache_file(path)
    return value


def _touch_cache_file(path: Path) -> None:
    """Update LRU evidence without following links outside the app cache."""

    root = cache_root().resolve()
    candidate = path.absolute()
    try:
        if (
            path.is_symlink()
            or not candidate.is_relative_to(cache_root().absolute())
            or not path.resolve().is_relative_to(root)
        ):
            return
        if time.time() - path.stat().st_mtime < _EVIDENCE_TOUCH_INTERVAL_SECONDS:
            return
        path.touch()
    except OSError:
        return


def read_indexed_pass_receipt(
    repository: Repository,
    profile: str,
    runner: str,
    resource_limits: ResourceLimits,
    test_network: str = "none",
    component: str = ".",
) -> tuple[dict, Path] | None:
    """Read a compatible PASS receipt before cloning the repository."""

    if test_network not in {"none", "enabled"}:
        return None
    component = normalize_component(component)
    indexes = [
        _index_path(
            repository,
            profile,
            runner,
            resource_limits,
            test_network,
            component,
        )
    ]
    if test_network == "none":
        indexes.append(
            _index_path(
                repository,
                profile,
                runner,
                resource_limits,
                None,
                component,
            )
        )
    for index in indexes:
        try:
            value = json.loads(index.read_text(encoding="utf-8"))
            relative = value["receipt"]
            if not isinstance(relative, str):
                continue
            root = cache_root().resolve()
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                continue
            receipt = read_pass_receipt(
                path,
                runner=runner,
                resource_limits=resource_limits,
                touch=False,
            )
        except (OSError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if not receipt:
            continue
        plan = receipt.get("plan")
        if not isinstance(plan, dict):
            continue
        repo = plan.get("repository")
        steps = plan.get("steps")
        if not isinstance(repo, dict) or not isinstance(steps, list):
            continue
        observed_network = (
            "enabled"
            if any(
                isinstance(step, dict)
                and step.get("test_network") == "enabled"
                for step in steps
            )
            else "none"
        )
        compatible = (
            receipt.get("receipt_version") == RECEIPT_VERSION
            and receipt.get("execution_policy_version") == EXECUTION_POLICY_VERSION
            and receipt.get("runner") == runner
            and plan.get("plan_version") == PLAN_VERSION
            and plan.get("profile") == profile
            and plan.get("component") == component
            and repo.get("commit_sha") == repository.commit_sha
            and repo.get("display_name") == repository.display_name
            and receipt.get("resource_limits") == resource_limits.to_dict()
            and observed_network == test_network
        )
        if not compatible:
            continue
        _touch_cache_file(path)
        _touch_cache_file(index)
        return receipt, path
    return None


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_receipt(path: Path, receipt: Receipt) -> None:
    _atomic_json(path, receipt.to_dict())
    if receipt.status != "pass":
        return
    root = cache_root().resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(root):
        return
    test_network = (
        "enabled"
        if any(
            step.test_network == "enabled"
            for step in receipt.plan.steps
        )
        else "none"
    )
    index = _index_path(
        receipt.plan.repository,
        receipt.plan.profile,
        receipt.runner,
        receipt.resource_limits,
        test_network,
        receipt.plan.component,
    )
    _atomic_json(
        index,
        {
            "receipt": resolved_path.relative_to(root).as_posix(),
            "commit_sha": receipt.plan.repository.commit_sha,
            "profile": receipt.plan.profile,
            "component": receipt.plan.component,
            "runner": receipt.runner,
            "resource_limits": receipt.resource_limits.to_dict(),
            "test_network": test_network,
            "plan_version": receipt.plan.plan_version,
            "execution_policy_version": receipt.execution_policy_version,
        },
    )
