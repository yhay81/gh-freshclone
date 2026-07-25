from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from gh_freshclone.github import resolve_repository
from gh_freshclone.model import BaselinePlan, Receipt, ResourceLimits
from gh_freshclone.receipts import receipt_path, write_receipt
from gh_freshclone.workflow import check_repository


@dataclass(frozen=True)
class BenchmarkResult:
    iterations: int
    minimum_ms: float
    median_ms: float
    p95_ms: float
    maximum_ms: float


def _percentile(samples: list[float], proportion: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * proportion + 0.999) - 1))
    return ordered[index]


def measure_cached_workflow(
    target: str,
    *,
    runner: str,
    iterations: int,
) -> BenchmarkResult:
    if iterations < 1:
        raise ValueError("iterations must be at least one")
    previous_cache = os.environ.get("GH_FRESHCLONE_CACHE")
    with tempfile.TemporaryDirectory(prefix="gh-freshclone-benchmark-") as temporary:
        os.environ["GH_FRESHCLONE_CACHE"] = str(Path(temporary) / "cache")
        try:
            repository = resolve_repository(target)
            plan = BaselinePlan(repository=repository, steps=())
            resource_limits = ResourceLimits()
            receipt = Receipt(
                created_at="2026-01-01T00:00:00+00:00",
                status="pass",
                runner=runner,
                runner_version="benchmark",
                host_platform="benchmark",
                plan=plan,
                resource_limits=resource_limits,
            )
            write_receipt(receipt_path(plan, runner, resource_limits), receipt)

            samples: list[float] = []
            for _ in range(iterations):
                started = time.perf_counter()
                cached_receipt, _, cached = check_repository(
                    target,
                    runner=runner,
                    echo=False,
                )
                elapsed = (time.perf_counter() - started) * 1000
                if not cached or cached_receipt["status"] != "pass":
                    raise RuntimeError("hot-path benchmark missed the seeded PASS index")
                samples.append(elapsed)
        finally:
            if previous_cache is None:
                os.environ.pop("GH_FRESHCLONE_CACHE", None)
            else:
                os.environ["GH_FRESHCLONE_CACHE"] = previous_cache

    ordered = sorted(samples)
    return BenchmarkResult(
        iterations=iterations,
        minimum_ms=round(ordered[0], 3),
        median_ms=round(_percentile(ordered, 0.5), 3),
        p95_ms=round(_percentile(ordered, 0.95), 3),
        maximum_ms=round(ordered[-1], 3),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure the clone-free cached PASS workflow."
    )
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--runner", default="docker")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--max-p95-ms", type=float, default=250)
    args = parser.parse_args()
    result = measure_cached_workflow(
        args.target,
        runner=args.runner,
        iterations=args.iterations,
    )
    print(json.dumps(asdict(result), sort_keys=True))
    if result.p95_ms > args.max_p95_ms:
        print(
            f"cached workflow p95 {result.p95_ms:.3f} ms exceeds "
            f"{args.max_p95_ms:.3f} ms"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
