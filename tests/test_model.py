from __future__ import annotations

import math

import pytest

from gh_freshclone.model import CheckStep, ResourceLimits, normalize_component


@pytest.mark.parametrize("cpus", [0, -1, math.inf, -math.inf, math.nan])
def test_resource_limits_reject_non_positive_or_non_finite_cpu(cpus: float) -> None:
    with pytest.raises(ValueError, match="finite number greater than zero"):
        ResourceLimits(cpus=cpus)


def test_resource_limits_reject_boolean_cpu() -> None:
    with pytest.raises(TypeError, match="not a boolean"):
        ResourceLimits(cpus=True)


def test_resource_limits_reject_non_string_memory() -> None:
    with pytest.raises(TypeError, match="memory must be a string"):
        ResourceLimits(memory=8)  # type: ignore[arg-type]


@pytest.mark.parametrize("memory", ["", "0g", "-1g", "8 gigabytes", "unlimited"])
def test_resource_limits_reject_non_canonical_memory(memory: str) -> None:
    with pytest.raises(ValueError, match="invalid memory limit"):
        ResourceLimits(memory=memory)


def test_resource_limits_canonicalize_memory_case_and_whitespace() -> None:
    limits = ResourceLimits(cpus=2.5, memory=" 4GiB ")

    assert limits.to_dict() == {"cpus": 2.5, "memory": "4gib"}


def test_resource_limits_canonicalize_integral_cpu_to_float() -> None:
    limits = ResourceLimits(cpus=4)

    assert limits.cpus == 4.0
    assert isinstance(limits.cpus, float)


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute", "windows\\path", "drive:path", ""],
)
def test_check_step_rejects_non_portable_working_directory(path: str) -> None:
    with pytest.raises(ValueError, match="working_directory"):
        CheckStep("custom", "alpine:3", "true", working_directory=path)


def test_check_step_canonicalizes_working_directory() -> None:
    step = CheckStep(
        "custom",
        "alpine:3",
        "true",
        working_directory="services/./api",
    )

    assert step.working_directory == "services/api"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (".", "."),
        ("apps/web", "apps/web"),
        ("apps/./web/", "apps/web"),
    ],
)
def test_component_path_is_canonicalized(value: str, expected: str) -> None:
    assert normalize_component(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../outside",
        "apps/../../outside",
        "/absolute",
        "windows\\path",
        "drive:path",
        ".git",
        "apps/.git/hooks",
        "apps/\nweb",
    ],
)
def test_component_path_must_be_portable_and_repository_relative(value: str) -> None:
    with pytest.raises(ValueError, match="component"):
        normalize_component(value)
