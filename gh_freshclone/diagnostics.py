from __future__ import annotations

import re

from .model import Diagnostic

_PACKAGE_HINTS = {
    "bash": "bash",
    "cc": "build-essential",
    "cmake": "cmake",
    "gcc": "build-essential",
    "git": "git",
    "less": "less",
    "make": "make",
    "openssl": "openssl",
    "pkg-config": "pkg-config",
}

_MISSING_EXECUTABLE_PATTERNS = (
    re.compile(r"(?:No such file or directory|FileNotFoundError):? ['\"](?P<name>[^/'\"]+)['\"]"),
    re.compile(r"Executable [`'\"](?P<name>[^`'\"]+)[`'\"] not found", re.IGNORECASE),
    re.compile(r"(?P<name>[A-Za-z0-9_.+-]+): (?:command )?not found", re.IGNORECASE),
)
_SHARED_LIBRARY = re.compile(
    r"error while loading shared libraries: (?P<name>[^: ]+)",
    re.IGNORECASE,
)


def failure_executable_candidates(line: str) -> tuple[str, ...]:
    """Return known executable names mentioned by a failing test selector."""

    if "failed" not in line.lower():
        return ()
    return tuple(
        name
        for name in _PACKAGE_HINTS
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", line)
    )


def diagnose_failure(
    returncode: int,
    detail: str,
    *,
    observed_missing_executables: tuple[str, ...] = (),
    test_network: str = "enabled",
    failed_phase: str | None = None,
) -> tuple[str, tuple[Diagnostic, ...]]:
    """Classify a runner failure and preserve actionable environment gaps."""

    if returncode == 0:
        return "pass", ()

    lowered = detail.lower()
    if "read-only file system" in lowered and any(
        marker in lowered
        for marker in (
            "containerd",
            "docker",
            "metadata",
            "meta.db",
        )
    ):
        return (
            "infra_failure",
            (
                Diagnostic(
                    kind="runner_storage",
                    message=(
                        "The container runner's data store became read-only. "
                        "Free host storage, restart or repair the runner, then retry."
                    ),
                    confidence="high",
                    evidence=("runner metadata write failed on a read-only filesystem",),
                ),
            ),
        )
    if returncode == 125 or any(
        marker in lowered
        for marker in (
            "cannot connect to the docker daemon",
            "error during connect",
            "container system is not running",
            "unable to find image",
            "manifest unknown",
        )
    ):
        return (
            "infra_failure",
            (
                Diagnostic(
                    kind="runner_infrastructure",
                    message="The container runner could not execute the baseline.",
                    confidence="high",
                    evidence=("runner exit status or daemon error",),
                ),
            ),
        )

    if failed_phase == "prepare" and any(
        marker in lowered
        for marker in (
            "failed to read from the distribution cache",
            "failed to rename file",
            "network is unreachable",
            "temporary failure in name resolution",
            "could not resolve host",
            "connection timed out",
            "connection reset by peer",
        )
    ):
        return (
            "infra_failure",
            (
                Diagnostic(
                    kind="dependency_preparation_infrastructure",
                    message=(
                        "Dependency preparation failed because its cache or "
                        "network infrastructure was unavailable."
                    ),
                    confidence="high",
                    evidence=("prepare-phase output reports an infrastructure error",),
                ),
            ),
        )

    if test_network == "none" and any(
        marker in lowered
        for marker in (
            "network is unreachable",
            "temporary failure in name resolution",
            "could not resolve host",
            "name or service not known",
            "failed to lookup address information",
            "dns error:",
            "enetunreach",
            "eai_again",
            "getaddrinfo",
        )
    ):
        return (
            "environment_gap",
            (
                Diagnostic(
                    kind="network_policy",
                    message=(
                        "The test attempted network access while its declared "
                        "network policy was 'none'."
                    ),
                    confidence="high",
                    evidence=("runner output reports a network access failure",),
                ),
            ),
        )

    found: dict[str, Diagnostic] = {}
    for pattern in _MISSING_EXECUTABLE_PATTERNS:
        for match in pattern.finditer(detail):
            name = match.group("name").strip()
            if "/" in name or "\\" in name:
                continue
            key = name.lower()
            found.setdefault(
                key,
                Diagnostic(
                    kind="missing_executable",
                    subject=name,
                    suggested_package=_PACKAGE_HINTS.get(key),
                    message=f"Required executable is missing: {name}",
                    confidence="high",
                    evidence=("runner output explicitly reports the executable missing",),
                ),
            )

    for match in _SHARED_LIBRARY.finditer(detail):
        name = match.group("name")
        found.setdefault(
            name.lower(),
            Diagnostic(
                kind="missing_shared_library",
                subject=name,
                message=f"Required shared library is missing: {name}",
                confidence="high",
                evidence=("runner output explicitly reports the shared library missing",),
            ),
        )

    if "permissionerror" in lowered and not found:
        found["permission"] = Diagnostic(
            kind="environment_permission",
            message="A tool installed by the repository could not be executed.",
            confidence="medium",
            evidence=("runner output contains PermissionError",),
        )

    for name in observed_missing_executables:
        key = name.lower()
        found.setdefault(
            key,
            Diagnostic(
                kind="missing_executable",
                subject=name,
                suggested_package=_PACKAGE_HINTS.get(key),
                message=(
                    f"Failing tests reference an executable absent from the image: {name}"
                ),
                confidence="medium",
                evidence=(
                    f"failed test selector references {name}",
                    f"content-addressed image probe could not find {name}",
                ),
            ),
        )

    if found or returncode in {126, 127}:
        diagnostics = tuple(found.values()) or (
            Diagnostic(
                kind="missing_executable",
                message="A required command could not be executed.",
                confidence="medium",
                evidence=(f"runner exited with status {returncode}",),
            ),
        )
        return "environment_gap", diagnostics
    return "test_failure", ()
