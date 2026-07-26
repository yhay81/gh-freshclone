from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

PLAN_VERSION = 8
RECEIPT_VERSION = 6
EXECUTION_POLICY_VERSION = 18
_MEMORY_LIMIT = re.compile(
    r"[1-9]\d*(?:\.\d+)?(?:[bkmg]i?b?|b)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CheckStep:
    """One deterministic ecosystem check executed in an isolated container."""

    ecosystem: str
    image: str
    command: str
    evidence: tuple[str, ...] = ()
    dependency_fingerprint: str = ""
    prepare_command: str = ""
    test_network: str = "none"
    working_directory: str = "."

    def __post_init__(self) -> None:
        if (
            not isinstance(self.working_directory, str)
            or not self.working_directory
            or "\\" in self.working_directory
            or ":" in self.working_directory
        ):
            raise ValueError("working_directory must be a non-empty POSIX path")
        path = PurePosixPath(self.working_directory)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("working_directory must stay within the repository")
        object.__setattr__(self, "working_directory", path.as_posix())

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = list(self.evidence)
        return value


@dataclass(frozen=True)
class Repository:
    """A GitHub or local Git repository pinned to an exact commit."""

    display_name: str
    commit_sha: str
    ref: str
    source_url: str | None
    github_repository: str | None
    local_path: str | None
    is_private: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BaselinePlan:
    """A reproducible plan inferred from repository-owned configuration."""

    repository: Repository
    steps: tuple[CheckStep, ...]
    profile: str = "quick"
    warnings: tuple[str, ...] = ()
    plan_version: int = PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "profile": self.profile,
            "repository": self.repository.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ResourceLimits:
    """Canonical user-controlled limits that form part of execution identity."""

    cpus: float = 4
    memory: str = "8g"

    def __post_init__(self) -> None:
        if isinstance(self.cpus, bool):
            raise TypeError("cpus must be a number, not a boolean")
        canonical_cpus = float(self.cpus)
        if not math.isfinite(canonical_cpus) or canonical_cpus <= 0:
            raise ValueError("cpus must be a finite number greater than zero")
        if not isinstance(self.memory, str):
            raise TypeError("memory must be a string")
        canonical_memory = self.memory.strip().lower()
        if not _MEMORY_LIMIT.fullmatch(canonical_memory):
            raise ValueError(f"invalid memory limit: {self.memory}")
        object.__setattr__(self, "cpus", canonical_cpus)
        object.__setattr__(self, "memory", canonical_memory)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Diagnostic:
    """A structured explanation for a failed baseline."""

    kind: str
    message: str
    subject: str | None = None
    suggested_package: str | None = None
    confidence: str = "medium"
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StepResult:
    ecosystem: str
    image: str
    image_identity: str
    command: str
    status: str
    exit_code: int | None
    duration_seconds: float
    log_path: str
    detail: str = ""
    diagnostics: tuple[Diagnostic, ...] = ()
    dependency_cache: str | None = None
    prepare_duration_seconds: float = 0
    test_network: str = "none"
    failed_phase: str | None = None
    prepared_volume: str | None = None
    prepare_cache_hit: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["diagnostics"] = [
            diagnostic.to_dict() for diagnostic in self.diagnostics
        ]
        return value


@dataclass(frozen=True)
class Receipt:
    created_at: str
    status: str
    runner: str
    runner_version: str
    host_platform: str
    plan: BaselinePlan
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    results: tuple[StepResult, ...] = field(default_factory=tuple)
    receipt_version: int = RECEIPT_VERSION
    execution_policy_version: int = EXECUTION_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_version": self.receipt_version,
            "execution_policy_version": self.execution_policy_version,
            "created_at": self.created_at,
            "status": self.status,
            "runner": self.runner,
            "runner_version": self.runner_version,
            "host_platform": self.host_platform,
            "resource_limits": self.resource_limits.to_dict(),
            "plan": self.plan.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }
