from __future__ import annotations

import shlex
from typing import Any

from ..api import ProbeOutcome
from ..model import BaselinePlan

TSUMUGI_ADAPTER_VERSION = 4

_BASELINE_CODE_PAIRS = (
    ("pass", "ok"),
    ("test_failure", "baseline_failed"),
    ("environment_gap", "toolchain_missing"),
    ("infra_failure", "baseline_infra"),
)
_BASELINE_CODES = dict(_BASELINE_CODE_PAIRS)


def _in_working_directory(command: str, working_directory: str = ".") -> str:
    if working_directory == ".":
        return command
    return f"(cd {shlex.quote(working_directory)} && {command})"


def plan_to_tsumugi_flags(plan: BaselinePlan) -> dict[str, Any]:
    """Translate compiler output to Tsumugi's existing repository flags."""

    command = " && ".join(
        _in_working_directory(command, step.working_directory)
        for step in plan.steps
        for command in (step.prepare_command, step.command)
        if command
    )
    return {
        "test_cmd": command or None,
        "test_cmd_source": "gh-freshclone",
        "test_cmd_probe_version": (
            f"gh-freshclone-plan-v{plan.plan_version}/"
            f"tsumugi-adapter-v{TSUMUGI_ADAPTER_VERSION}"
        ),
        "baseline_profile": plan.profile,
        "baseline_compiler_evidence": [
            {
                "ecosystem": step.ecosystem,
                "image": step.image,
                "evidence": list(step.evidence),
                "dependency_fingerprint": step.dependency_fingerprint,
                "prepare_command": step.prepare_command,
                "test_network": step.test_network,
                "working_directory": step.working_directory,
            }
            for step in plan.steps
        ],
    }


def outcome_to_tsumugi_flags(outcome: ProbeOutcome) -> dict[str, Any]:
    """Translate a probe receipt without weakening Tsumugi's own safety controls."""

    payload = outcome.to_dict()
    plan = payload["plan"]
    status = str(payload["status"])
    results = payload.get("results", [])
    detail = ""
    if results:
        last = results[-1]
        diagnostics = last.get("diagnostics") or []
        if diagnostics:
            detail = "; ".join(str(item.get("message", "")) for item in diagnostics)
        else:
            detail = str(last.get("detail") or "")
    if not detail:
        detail = "baseline passed" if status == "pass" else f"baseline {status}"

    flags = {
        "test_cmd": " && ".join(
            _in_working_directory(
                str(command),
                str(step.get("working_directory", ".")),
            )
            for step in plan["steps"]
            for command in (step.get("prepare_command"), step["command"])
            if command
        )
        or None,
        "test_cmd_source": "gh-freshclone",
        "test_cmd_probe_version": (
            f"gh-freshclone-plan-v{plan['plan_version']}/"
            f"tsumugi-adapter-v{TSUMUGI_ADAPTER_VERSION}"
        ),
        "baseline_ok": status == "pass",
        "baseline_code": _BASELINE_CODES.get(status, "baseline_failed"),
        "baseline_detail": detail[-500:],
        "baseline_base_sha": plan["repository"]["commit_sha"],
        "baseline_profile": plan["profile"],
        "baseline_probe_version": (
            f"gh-freshclone-receipt-v{payload['receipt_version']}/"
            f"execution-v{payload['execution_policy_version']}"
        ),
        "baseline_receipt_path": payload["receipt_path"],
        "baseline_receipt_cached": payload["cached"],
        "baseline_resource_limits": payload["resource_limits"],
        "baseline_compiler_evidence": [
            {
                "ecosystem": result["ecosystem"],
                "image_identity": result["image_identity"],
                "diagnostics": result["diagnostics"],
                "dependency_cache": result["dependency_cache"],
                "prepare_duration_seconds": result.get(
                    "prepare_duration_seconds",
                    0,
                ),
                "test_network": result.get("test_network", "none"),
                "failed_phase": result.get("failed_phase"),
                "prepared_volume": result.get("prepared_volume"),
                "prepare_cache_hit": result.get("prepare_cache_hit"),
            }
            for result in results
        ],
    }
    return flags
