from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StartupResult:
    iterations: int
    minimum_ms: float
    median_ms: float
    p95_ms: float
    maximum_ms: float


def _percentile(samples: list[float], proportion: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * proportion + 0.999) - 1))
    return ordered[index]


def measure_startup(command: list[str], *, iterations: int) -> StartupResult:
    if iterations < 1:
        raise ValueError("iterations must be at least one")
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"startup command exited with {completed.returncode}: {command[0]}"
            )
        samples.append((time.perf_counter() - started) * 1000)

    ordered = sorted(samples)
    return StartupResult(
        iterations=iterations,
        minimum_ms=round(ordered[0], 3),
        median_ms=round(_percentile(ordered, 0.5), 3),
        p95_ms=round(_percentile(ordered, 0.95), 3),
        maximum_ms=round(ordered[-1], 3),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure installed gh-freshclone --version startup."
    )
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--max-p95-ms", type=float, default=250)
    args = parser.parse_args()
    executable = shutil.which("gh-freshclone")
    if not executable:
        raise SystemExit("gh-freshclone is not installed on PATH")
    result = measure_startup(
        [executable, "--version"],
        iterations=args.iterations,
    )
    print(json.dumps(asdict(result), sort_keys=True))
    if result.p95_ms > args.max_p95_ms:
        print(
            f"CLI startup p95 {result.p95_ms:.3f} ms exceeds "
            f"{args.max_p95_ms:.3f} ms"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
