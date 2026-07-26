from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from . import __version__
from .constants import (
    API_VERSION,
    APPLE_CONTAINER_MIN_VERSION,
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_EVIDENCE_BYTES,
    DEFAULT_MAX_EVIDENCE_ENTRIES,
    DEFAULT_MAX_VOLUME_BYTES,
    DEFAULT_MAX_VOLUMES,
    PROFILES,
    TEST_NETWORK_POLICIES,
)

if TYPE_CHECKING:
    from .model import BaselinePlan, Receipt


def create_plan(*args: Any, **kwargs: Any) -> BaselinePlan:
    from .workflow import create_plan as implementation

    return implementation(*args, **kwargs)


def probe_repository(*args: Any, **kwargs: Any):
    from .api import probe_repository as implementation

    return implementation(*args, **kwargs)


def cache_status():
    from .cache import cache_status as implementation

    return implementation()


def prune_cache(**kwargs: Any):
    from .cache import prune_cache as implementation

    return implementation(**kwargs)


def github_status(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from .github_status import github_status as implementation

    return implementation(*args, **kwargs)


def available_runners():
    from .runner_policy import available_runners as implementation

    return implementation()


def runner_ready(name: str) -> bool:
    from .runner_policy import runner_ready as implementation

    return implementation(name)


def runner_supported(name: str, version: str | None) -> bool:
    from .runner_policy import runner_supported as implementation

    return implementation(name, version)


def _which(command: str) -> str | None:
    import shutil

    return shutil.which(command)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gh-freshclone",
        description=(
            "Prove that a GitHub repository passes its baseline checks "
            "in a clean OCI container."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="infer a baseline without executing it")
    plan.add_argument(
        "target",
        help="OWNER/REPO, GitHub repository/commit/pull URL, or local Git path",
    )
    plan.add_argument("--ref", help="branch, tag, or commit (default: default branch/HEAD)")
    plan.add_argument(
        "--profile",
        choices=PROFILES,
        default="quick",
        help="quick baseline, repository-default reproduction, or full checks",
    )
    plan.add_argument(
        "--component",
        default=".",
        help=(
            "explicit repository-relative component directory "
            "(default: repository root)"
        ),
    )
    plan.add_argument(
        "--test-network",
        choices=TEST_NETWORK_POLICIES,
        default="none",
        help=(
            "test-container network policy; outbound access requires "
            "the explicit 'enabled' opt-in"
        ),
    )
    plan.add_argument("--json", action="store_true", help="print machine-readable JSON")

    check = subparsers.add_parser("check", help="run the inferred baseline in a container")
    check.add_argument(
        "target",
        help="OWNER/REPO, GitHub repository/commit/pull URL, or local Git path",
    )
    check.add_argument("--ref", help="branch, tag, or commit (default: default branch/HEAD)")
    check.add_argument(
        "--runner",
        choices=("auto", "docker", "podman", "container"),
        default="auto",
        help="OCI runner; macOS auto prefers Apple container",
    )
    check.add_argument("--cpus", type=float, default=4, help="CPU limit (default: 4)")
    check.add_argument("--memory", default="8g", help="memory limit (default: 8g)")
    check.add_argument(
        "--profile",
        choices=PROFILES,
        default="quick",
        help="quick baseline (default), reproduce, or full",
    )
    check.add_argument(
        "--component",
        default=".",
        help=(
            "explicit repository-relative component directory "
            "(default: repository root)"
        ),
    )
    check.add_argument(
        "--test-network",
        choices=TEST_NETWORK_POLICIES,
        default="none",
        help=(
            "test-container network policy; outbound access requires "
            "the explicit 'enabled' opt-in"
        ),
    )
    check.add_argument("--no-cache", action="store_true", help="ignore a passing receipt")
    check.add_argument("--json", action="store_true", help="print machine-readable JSON")
    check.add_argument(
        "--quiet",
        action="store_true",
        help="do not stream container output; it is still written to logs",
    )

    status = subparsers.add_parser(
        "github-status",
        help="read GitHub Checks and commit statuses for the exact commit",
    )
    status.add_argument(
        "target",
        help="public OWNER/REPO or GitHub repository/commit/pull URL",
    )
    status.add_argument(
        "--ref",
        help="branch, tag, or commit (default: default branch)",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON",
    )

    cache = subparsers.add_parser("cache", help="inspect or prune app-owned caches")
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_status_parser = cache_commands.add_parser("status", help="show cache usage")
    cache_status_parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )
    cache_prune = cache_commands.add_parser(
        "prune", help="safely enforce cache limits"
    )
    cache_prune.add_argument(
        "--max-gib", type=float, default=DEFAULT_MAX_BYTES / 1024**3
    )
    cache_prune.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    cache_prune.add_argument(
        "--max-evidence-gib",
        type=float,
        default=DEFAULT_MAX_EVIDENCE_BYTES / 1024**3,
    )
    cache_prune.add_argument(
        "--max-evidence-entries",
        type=int,
        default=DEFAULT_MAX_EVIDENCE_ENTRIES,
    )
    cache_prune.add_argument(
        "--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS
    )
    cache_prune.add_argument("--max-volumes", type=int, default=DEFAULT_MAX_VOLUMES)
    cache_prune.add_argument(
        "--max-volume-gib",
        type=float,
        default=DEFAULT_MAX_VOLUME_BYTES / 1024**3,
    )
    cache_prune.add_argument(
        "--json", action="store_true", help="print machine-readable JSON"
    )

    doctor = subparsers.add_parser(
        "doctor", help="show Git and container prerequisites"
    )
    doctor.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def _print_plan(plan: BaselinePlan) -> None:
    repo = plan.repository
    print(f"Repository: {repo.display_name}")
    print(f"Commit:     {repo.commit_sha}")
    print(f"Ref:        {repo.ref}")
    print(f"Profile:    {plan.profile}")
    if plan.component != ".":
        print(f"Component:  {plan.component}")
    if not plan.steps:
        print("Checks:     none")
    for index, step in enumerate(plan.steps, start=1):
        print(f"\n[{index}] {step.ecosystem}")
        if step.working_directory != ".":
            print(f"Directory:  {step.working_directory}")
        print(f"Image:      {step.image}")
        if step.prepare_command:
            print(f"Prepare:    {step.prepare_command}")
        print(f"Test:       {step.command}")
        print(f"Test net:   {step.test_network}")
        print(f"Evidence:   {', '.join(step.evidence)}")
    for warning in plan.warnings:
        print(f"\nWarning: {warning}")


def _print_initialization_error(exc: BaseException, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "api_version": API_VERSION,
                    "status": "error",
                    "error": {
                        "code": "initialization_error",
                        "message": str(exc),
                    },
                },
                ensure_ascii=True,
                indent=2,
            )
        )
    else:
        print(f"gh-freshclone: {exc}", file=sys.stderr)


def _receipt_dict(value: Receipt | dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else value.to_dict()


def _print_receipt(
    value: Receipt | dict[str, Any],
    path: str,
    cached: bool,
    elapsed_seconds: float,
) -> None:
    payload = _receipt_dict(value)
    plan = payload["plan"]
    repository = plan["repository"]
    print(f"Repository: {repository['display_name']}")
    print(f"Commit:     {repository['commit_sha']}")
    print(f"Result:     {payload['status'].upper()}{' (cached)' if cached else ''}")
    print(f"Runner:     {payload['runner']}")
    print(f"Profile:    {plan.get('profile', 'quick')}")
    source_validation = payload.get("source_validation")
    if source_validation:
        source_suffix = (
            " (cache hit)"
            if payload.get("source_cache_hit")
            else ""
        )
        print(f"Source:     {source_validation}{source_suffix}")
    limits = payload.get("resource_limits", {})
    if limits:
        cpus = limits.get("cpus", "?")
        rendered_cpus = f"{cpus:g}" if isinstance(cpus, (int, float)) else str(cpus)
        print(
            f"Resources:  {rendered_cpus} CPU, "
            f"{limits.get('memory', '?')} memory"
        )
    for result in payload.get("results", []):
        print(
            f"  {result['ecosystem']}: {result['status']} "
            f"({result['duration_seconds']:.1f}s)"
        )
        print(
            f"    prepare={result.get('prepare_duration_seconds', 0):.1f}s, "
            f"test-network={result.get('test_network', 'none')}"
        )
        for diagnostic in result.get("diagnostics", []):
            suggestion = diagnostic.get("suggested_package")
            suffix = f" (suggested package: {suggestion})" if suggestion else ""
            print(f"    {diagnostic['message']}{suffix}")
    print(f"Elapsed:    {elapsed_seconds:.1f}s")
    print(f"Receipt:    {path}")


def _doctor(*, json_output: bool = False) -> int:
    import platform
    from concurrent.futures import ThreadPoolExecutor

    git_path = _which("git")
    runners = available_runners()
    supported_runners = {
        name: version
        for name, version in runners.items()
        if bool(version) and runner_supported(name, version)
    }
    readiness: dict[str, bool] = {}
    if supported_runners:
        with ThreadPoolExecutor(
            max_workers=len(supported_runners),
            thread_name_prefix="ghfc-doctor-ready",
        ) as executor:
            futures = {
                name: executor.submit(runner_ready, name)
                for name in supported_runners
            }
            readiness = {
                name: future.result()
                for name, future in futures.items()
            }
    runner_reports: dict[str, dict[str, str | bool | None]] = {}
    for name, version in runners.items():
        supported = bool(version) and runner_supported(name, version)
        ready = supported and readiness.get(name, False)
        report: dict[str, str | bool | None] = {
            "version": version or None,
            "supported": supported,
            "ready": ready,
        }
        if name == "container":
            report["minimum_version"] = ".".join(
                map(str, APPLE_CONTAINER_MIN_VERSION)
            )
        runner_reports[name] = report
    ready = bool(git_path) and any(
        bool(report["ready"]) for report in runner_reports.values()
    )
    payload = {
        "ready": ready,
        "host": platform.platform(),
        "python": platform.python_version(),
        "git": {"path": git_path, "ready": bool(git_path)},
        "github": {
            "mode": "credential-free-https",
            "authentication_required": False,
        },
        "runners": runner_reports,
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0 if ready else 1

    print(f"Host:       {payload['host']}")
    print(f"Python:     {payload['python']}")
    print(f"{'git:':11}{git_path or 'not found'}")
    print(f"{'GitHub:':11}credential-free HTTPS (no API login)")
    for name, report in runner_reports.items():
        version = report["version"]
        supported = bool(report["supported"])
        runner_is_ready = bool(report["ready"])
        if not version:
            suffix = ""
        elif not supported:
            suffix = f" (unsupported; requires {report['minimum_version']}+)"
        elif not runner_is_ready:
            suffix = " (not ready)"
        else:
            suffix = ""
        print(f"{name + ':':11}{version or 'not found'}{suffix}")
    return 0 if ready else 1


def _print_cache_report(payload: dict[str, Any]) -> None:
    gib = payload["host_bytes"] / 1024**3
    evidence_gib = payload.get("evidence_bytes", 0) / 1024**3
    print(f"Host cache:       {gib:.2f} GiB in {payload['host_entries']} entries")
    print(
        f"Evidence:         {evidence_gib:.2f} GiB in "
        f"{payload.get('evidence_entries', 0)} bundles"
    )
    volume_bytes = payload.get("prepared_volume_bytes", 0)
    volume_size_complete = payload.get("prepared_volume_size_complete", False)
    if volume_size_complete:
        volume_detail = f"{volume_bytes / 1024**3:.2f} GiB"
    elif volume_bytes:
        volume_detail = f"at least {volume_bytes / 1024**3:.2f} GiB"
    else:
        volume_detail = "size unavailable"
    print(f"Prepared volumes: {payload['prepared_volumes']} ({volume_detail})")
    if payload.get("removed_entries") or payload.get("removed_volumes"):
        removed_gib = payload["removed_bytes"] / 1024**3
        print(
            f"Removed:          {removed_gib:.2f} GiB, "
            f"{payload['removed_entries']} entries, "
            f"{payload['removed_volumes']} volumes"
        )
    if payload.get("removed_evidence_entries"):
        removed_evidence_gib = payload["removed_evidence_bytes"] / 1024**3
        print(
            f"Removed evidence: {removed_evidence_gib:.2f} GiB, "
            f"{payload['removed_evidence_entries']} bundles"
        )
    if payload.get("removed_volume_bytes"):
        print(
            "Removed volume data: "
            f"{payload['removed_volume_bytes'] / 1024**3:.2f} GiB"
        )
    for warning in payload.get("warnings", ()):
        print(f"Warning: {warning}")


def _print_github_status(payload: dict[str, Any]) -> None:
    repository = payload["repository"]
    checks = payload["checks"]
    legacy = payload["legacy_status"]
    print(f"Repository: {repository['full_name']}")
    print(f"Commit:     {payload['commit_sha']}")
    print(f"GitHub:     {payload['state']}")
    properties = []
    if repository.get("archived"):
        properties.append("archived")
    if repository.get("disabled"):
        properties.append("disabled")
    if repository.get("fork"):
        properties.append("fork")
    if properties:
        print(f"Repository flags: {', '.join(properties)}")
    counts = ", ".join(
        f"{value} {name}" for name, value in checks["counts"].items()
    )
    detail = f" ({counts})" if counts else ""
    print(
        f"Checks:     {checks['state']}; "
        f"{checks['total_count']} total{detail}"
    )
    for run in checks["runs"]:
        if (
            run["status"] == "completed"
            and run["conclusion"] in _SUCCESSFUL_CHECK_CONCLUSIONS
        ):
            continue
        result = run["conclusion"] or run["status"]
        print(f"  {run['name']}: {result}")
    print(
        f"Statuses:   {legacy['state']}; "
        f"{legacy['total_count']} contexts"
    )
    rate = payload.get("rate_limit")
    if isinstance(rate, dict) and "remaining" in rate:
        reset = f", resets {rate['reset_at']}" if rate.get("reset_at") else ""
        limit = f"/{rate['limit']}" if rate.get("limit") is not None else ""
        print(f"API quota:  {rate['remaining']}{limit} remaining{reset}")


_SUCCESSFUL_CHECK_CONCLUSIONS = {"neutral", "skipped", "success"}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        return _doctor(json_output=args.json)
    if args.command == "github-status":
        from .github import RepositoryError
        from .github_status import GitHubStatusError

        try:
            payload = github_status(args.target, args.ref)
            if args.json:
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                _print_github_status(payload)
            return 0
        except (GitHubStatusError, RepositoryError, ValueError) as exc:
            _print_initialization_error(exc, json_output=args.json)
            return 2
    if args.command == "cache":
        try:
            if args.cache_command == "status":
                report = cache_status()
            else:
                if (
                    args.max_gib < 0
                    or args.max_evidence_gib < 0
                    or args.max_volume_gib < 0
                ):
                    raise ValueError("cache size limits cannot be negative")
                report = prune_cache(
                    max_bytes=int(args.max_gib * 1024**3),
                    max_entries=args.max_entries,
                    max_evidence_bytes=int(args.max_evidence_gib * 1024**3),
                    max_evidence_entries=args.max_evidence_entries,
                    max_age_days=args.max_age_days,
                    max_volumes=args.max_volumes,
                    max_volume_bytes=int(args.max_volume_gib * 1024**3),
                )
            payload = report.to_dict()
            if args.json:
                print(json.dumps(payload, ensure_ascii=True, indent=2))
            else:
                _print_cache_report(payload)
            return 0
        except ValueError as exc:
            _print_initialization_error(exc, json_output=args.json)
            return 2
    if args.command == "plan":
        from .github import RepositoryError
        from .workflow import WorkflowError

        try:
            plan = create_plan(
                args.target,
                args.ref,
                profile=args.profile,
                test_network=args.test_network,
                component=args.component,
            )
            if args.json:
                print(json.dumps(plan.to_dict(), ensure_ascii=True, indent=2))
            else:
                _print_plan(plan)
            return 0 if plan.steps else 1
        except (RepositoryError, ValueError, WorkflowError) as exc:
            _print_initialization_error(exc, json_output=args.json)
            return 2
    if args.command == "check":
        from .github import RepositoryError
        from .runner_policy import RunnerError
        from .workflow import WorkflowError

        try:
            outcome = probe_repository(
                args.target,
                ref=args.ref,
                runner=args.runner,
                cpus=args.cpus,
                memory=args.memory,
                use_cache=not args.no_cache,
                echo=not args.quiet and not args.json,
                profile=args.profile,
                test_network=args.test_network,
                component=args.component,
            )
            receipt = outcome.receipt
            path = outcome.receipt_path
            cached = outcome.cached
            payload = _receipt_dict(receipt)
            if args.json:
                print(
                    json.dumps(
                        outcome.to_dict(),
                        ensure_ascii=True,
                        indent=2,
                    )
                )
            else:
                _print_receipt(
                    receipt,
                    str(path),
                    cached,
                    outcome.elapsed_seconds,
                )
            return 0 if payload.get("status") == "pass" else 1
        except (RepositoryError, RunnerError, ValueError, WorkflowError) as exc:
            _print_initialization_error(exc, json_output=args.json)
            return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
