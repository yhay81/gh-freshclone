from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from gh_freshclone.api import plan_repository
from gh_freshclone.model import BaselinePlan, normalize_component

DEFAULT_COHORT = Path(__file__).with_name("public-plan-cohort.json")
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_ECOSYSTEM = re.compile(r"[a-z][a-z0-9-]{0,31}")
_EXACT_COMMIT = re.compile(r"[0-9a-f]{40}")
_PUBLIC_TARGET = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9])?/"
    r"[A-Za-z0-9_.-]{1,100}"
)
_CASE_KEYS = {
    "id",
    "target",
    "ref",
    "component",
    "expected_ecosystems",
}


@dataclass(frozen=True)
class CohortCase:
    id: str
    target: str
    ref: str
    component: str
    expected_ecosystems: tuple[str, ...]


@dataclass(frozen=True)
class CohortCaseResult:
    id: str
    target: str
    ref: str
    component: str
    expected_ecosystems: tuple[str, ...]
    actual_ecosystems: tuple[str, ...]
    outcome: str
    duration_seconds: float
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["expected_ecosystems"] = list(self.expected_ecosystems)
        value["actual_ecosystems"] = list(self.actual_ecosystems)
        value["warnings"] = list(self.warnings)
        return value


Planner = Callable[..., BaselinePlan]


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _parse_case(value: object, index: int) -> CohortCase:
    if not isinstance(value, dict):
        raise TypeError(f"cases[{index}] must be an object")
    unknown = set(value) - _CASE_KEYS
    if unknown:
        raise ValueError(f"cases[{index}] has unknown fields: {', '.join(sorted(unknown))}")
    missing = {"id", "target", "ref", "expected_ecosystems"} - set(value)
    if missing:
        raise ValueError(f"cases[{index}] is missing fields: {', '.join(sorted(missing))}")

    case_id = _require_string(value["id"], f"cases[{index}].id")
    target = _require_string(value["target"], f"cases[{index}].target")
    ref = _require_string(value["ref"], f"cases[{index}].ref")
    component = normalize_component(
        _require_string(value.get("component", "."), f"cases[{index}].component")
    )
    expected_value = value["expected_ecosystems"]
    if not isinstance(expected_value, list) or len(expected_value) > 16:
        raise ValueError(f"cases[{index}].expected_ecosystems must be a list")
    expected = tuple(
        sorted(
            _require_string(item, f"cases[{index}].expected_ecosystems")
            for item in expected_value
        )
    )

    if not _CASE_ID.fullmatch(case_id):
        raise ValueError(f"cases[{index}].id is not a portable identifier")
    if not _PUBLIC_TARGET.fullmatch(target) or target.endswith(".git"):
        raise ValueError(f"cases[{index}].target must be public OWNER/REPO syntax")
    if not _EXACT_COMMIT.fullmatch(ref):
        raise ValueError(f"cases[{index}].ref must be a full lowercase commit SHA")
    if any(not _ECOSYSTEM.fullmatch(item) for item in expected):
        raise ValueError(f"cases[{index}].expected_ecosystems has an invalid name")
    if len(set(expected)) != len(expected):
        raise ValueError(f"cases[{index}].expected_ecosystems contains duplicates")
    return CohortCase(
        id=case_id,
        target=target,
        ref=ref,
        component=component,
        expected_ecosystems=expected,
    )


def load_cohort(path: Path) -> tuple[CohortCase, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("cohort schema_version must be 1")
    unknown = set(payload) - {"schema_version", "description", "cases"}
    if unknown:
        raise ValueError(f"cohort has unknown fields: {', '.join(sorted(unknown))}")
    values = payload.get("cases")
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise ValueError("cohort must contain 1-200 cases")
    cases = tuple(_parse_case(value, index) for index, value in enumerate(values))
    ids = [case.id for case in cases]
    identities = [(case.target.casefold(), case.ref, case.component) for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("cohort case ids must be unique")
    if len(set(identities)) != len(identities):
        raise ValueError("cohort target/ref/component identities must be unique")
    return cases


def _evaluate_case(case: CohortCase, planner: Planner) -> CohortCaseResult:
    started = time.perf_counter()
    try:
        plan = planner(
            case.target,
            ref=case.ref,
            component=case.component,
        )
    # A cohort report must retain arbitrary acquisition failures and continue
    # evaluating the remaining independent public targets.
    except Exception as exc:  # noqa: BLE001
        return CohortCaseResult(
            id=case.id,
            target=case.target,
            ref=case.ref,
            component=case.component,
            expected_ecosystems=case.expected_ecosystems,
            actual_ecosystems=(),
            outcome="error",
            duration_seconds=round(time.perf_counter() - started, 3),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )

    actual = tuple(sorted({step.ecosystem for step in plan.steps}))
    if actual == case.expected_ecosystems:
        outcome = "match"
    elif actual and not case.expected_ecosystems:
        outcome = "unexpected-plan"
    elif case.expected_ecosystems and not actual:
        outcome = "missed-plan"
    else:
        outcome = "ecosystem-mismatch"
    return CohortCaseResult(
        id=case.id,
        target=case.target,
        ref=case.ref,
        component=case.component,
        expected_ecosystems=case.expected_ecosystems,
        actual_ecosystems=actual,
        outcome=outcome,
        duration_seconds=round(time.perf_counter() - started, 3),
        warnings=plan.warnings,
    )


def _percentile(samples: list[float], proportion: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * proportion + 0.999) - 1))
    return ordered[index]


def evaluate_cohort(
    path: Path = DEFAULT_COHORT,
    *,
    jobs: int = 4,
    planner: Planner = plan_repository,
) -> dict[str, object]:
    if jobs < 1 or jobs > 32:
        raise ValueError("jobs must be between 1 and 32")
    cases = load_cohort(path)
    with ThreadPoolExecutor(max_workers=min(jobs, len(cases))) as executor:
        results = tuple(executor.map(lambda case: _evaluate_case(case, planner), cases))

    outcomes = Counter(result.outcome for result in results)
    durations = [result.duration_seconds for result in results]
    executable = sum(bool(result.actual_ecosystems) for result in results)
    expected_executable = sum(bool(case.expected_ecosystems) for case in cases)
    return {
        "schema_version": 1,
        "cohort_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "total": len(results),
        "matched": outcomes["match"],
        "regressions": len(results) - outcomes["match"],
        "outcomes": {
            name: outcomes[name]
            for name in (
                "match",
                "missed-plan",
                "unexpected-plan",
                "ecosystem-mismatch",
                "error",
            )
        },
        "expected_executable_plans": expected_executable,
        "executable_plans": executable,
        "detection_rate": round(executable / len(results), 4),
        "median_seconds": round(_percentile(durations, 0.5), 3),
        "p95_seconds": round(_percentile(durations, 0.95), 3),
        "cases": [result.to_dict() for result in results],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reproduce manifest-only planning across exact public commits."
    )
    parser.add_argument("--cohort", type=Path, default=DEFAULT_COHORT)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_cohort(args.cohort, jobs=args.jobs)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    return 1 if result["regressions"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
