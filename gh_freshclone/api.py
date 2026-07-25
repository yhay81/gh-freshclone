from __future__ import annotations

import json
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from .constants import API_VERSION
from .model import BaselinePlan, Receipt, Repository


@dataclass(frozen=True)
class ProbeOutcome:
    """Stable machine-facing result for agents and orchestration systems."""

    receipt: Receipt | dict[str, Any]
    receipt_path: Path
    cached: bool
    elapsed_seconds: float = 0
    api_version: int = API_VERSION

    @property
    def status(self) -> str:
        payload = self.receipt.to_dict() if isinstance(self.receipt, Receipt) else self.receipt
        return str(payload["status"])

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, Any]:
        payload = (
            self.receipt.to_dict() if isinstance(self.receipt, Receipt) else dict(self.receipt)
        )
        return {
            "api_version": self.api_version,
            **payload,
            "receipt_path": str(self.receipt_path),
            "cached": self.cached,
            "elapsed_seconds": self.elapsed_seconds,
        }


def plan_repository(
    target: str,
    ref: str | None = None,
    *,
    profile: str = "quick",
) -> BaselinePlan:
    """Compile a GitHub or local Git target into a deterministic baseline plan."""

    from .workflow import create_plan

    return create_plan(target, ref, profile=profile)


def compile_materialized_checkout(
    checkout: Path,
    repository: Repository,
    *,
    profile: str = "quick",
) -> BaselinePlan:
    """Compile an already materialized exact checkout without executing its code.

    The caller is responsible for ensuring that ``checkout`` contains only files
    from ``repository.commit_sha``. This lower-level entry point lets systems such
    as Tsumugi reuse the compiler while retaining their own sandbox and clone
    lifecycle.
    """

    from .detect import detect_plan

    return detect_plan(repository, checkout.resolve(), profile)


def probe_repository(
    target: str,
    *,
    ref: str | None = None,
    runner: str = "auto",
    cpus: float = 4,
    memory: str = "8g",
    use_cache: bool = True,
    echo: bool = True,
    profile: str = "quick",
) -> ProbeOutcome:
    """Compile and execute a baseline, returning a versioned typed outcome."""

    from .workflow import check_repository

    started = time.perf_counter()
    receipt, path, cached = check_repository(
        target,
        ref=ref,
        runner=runner,
        cpus=cpus,
        memory=memory,
        use_cache=use_cache,
        echo=echo,
        profile=profile,
    )
    return ProbeOutcome(
        receipt=receipt,
        receipt_path=path,
        cached=cached,
        elapsed_seconds=round(time.perf_counter() - started, 3),
    )


def receipt_schema() -> dict[str, Any]:
    """Load the bundled JSON Schema for the current receipt format."""

    resource = files("gh_freshclone.schemas").joinpath("receipt-v6.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))
