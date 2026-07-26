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
_SHARED_LIBRARY_PACKAGE_HINTS = {
    "libasound.so.2": "libasound2",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libcairo.so.2": "libcairo2",
    "libcups.so.2": "libcups2",
    "libdrm.so.2": "libdrm2",
    "libgbm.so.1": "libgbm1",
    "libgobject-2.0.so.0": "libglib2.0-0",
    "libnss3.so": "libnss3",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libX11.so.6": "libx11-6",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1",
    "libXext.so.6": "libxext6",
    "libXfixes.so.3": "libxfixes3",
    "libXrandr.so.2": "libxrandr2",
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
_DIAGNOSTIC_OUTPUT_MARKERS = (
    "command not found",
    "executable file not found",
    "no such file or directory",
    "permissionerror",
    "fatalerror",
    "executable `",
    "network is unreachable",
    "temporary failure in name resolution",
    "could not resolve host",
    "unknownhostexception",
    "name or service not known",
    "failed to lookup address information",
    "dns error:",
    "enetunreach",
    "eai_again",
    "getaddrinfo",
    "no cached version",
    "has not been downloaded",
    "could not parse the content at http",
    "terms of use have not been agreed to",
    "license agreement has not been accepted",
    "cannot find a java installation on your machine",
    "toolchain download repositories have not been configured",
    "it is missing from your system",
    "does not satisfy that requirement",
    "your lock file does not contain a compatible set of packages",
    "your ruby version is",
    "but your gemfile specified",
    "probably lack necessary libraries and/or headers",
    "could not find gem",
    "locally installed gems",
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


def is_diagnostic_output(line: str) -> bool:
    """Return whether a log line should survive beyond the ordinary output tail."""

    lowered = line.lower()
    return any(marker in lowered for marker in _DIAGNOSTIC_OUTPUT_MARKERS)


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
            "unknownhostexception",
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

    if any(
        marker in lowered
        for marker in (
            "cannot find a java installation on your machine",
            "toolchain download repositories have not been configured",
        )
    ):
        return (
            "environment_gap",
            (
                Diagnostic(
                    kind="missing_java_toolchain",
                    message=(
                        "The Gradle build requires a Java toolchain that is not "
                        "available in the selected container image."
                    ),
                    confidence="high",
                    evidence=("runner output explicitly reports a missing Java toolchain",),
                ),
            ),
        )

    composer_platform_gap = (
        failed_phase == "prepare"
        and (
            "it is missing from your system" in lowered
            or (
                "your php version" in lowered
                and "does not satisfy that requirement" in lowered
            )
        )
    )
    if composer_platform_gap:
        requirement = re.search(
            r"\brequires (?P<name>ext-[a-z0-9_.-]+)\b",
            lowered,
        )
        subject = requirement.group("name") if requirement else None
        return (
            "environment_gap",
            (
                Diagnostic(
                    kind="php_platform_requirement",
                    subject=subject,
                    message=(
                        "The locked Composer graph requires a PHP version or "
                        "extension absent from the selected multi-architecture image."
                    ),
                    confidence="high",
                    evidence=(
                        "Composer explicitly rejected a locked PHP platform requirement",
                    ),
                ),
            ),
        )

    ruby_version_gap = (
        "your ruby version is" in lowered
        and "but your gemfile specified" in lowered
    )
    ruby_native_gap = (
        "could not create makefile" in lowered
        and "probably lack necessary libraries and/or headers" in lowered
    )
    if ruby_version_gap or ruby_native_gap:
        return (
            "environment_gap",
            (
                Diagnostic(
                    kind=(
                        "ruby_runtime_requirement"
                        if ruby_version_gap
                        else "ruby_native_build_requirement"
                    ),
                    message=(
                        "The locked Ruby bundle requires a Ruby runtime or "
                        "native build dependency absent from the selected "
                        "multi-architecture image."
                    ),
                    confidence="high",
                    evidence=(
                        "Bundler or RubyGems explicitly rejected an image requirement",
                    ),
                ),
            ),
        )

    if any(
        marker in lowered
        for marker in (
            "terms of use have not been agreed to",
            "license agreement has not been accepted",
        )
    ):
        return (
            "environment_gap",
            (
                Diagnostic(
                    kind="external_agreement_required",
                    message=(
                        "The repository baseline requires an external agreement "
                        "that gh-freshclone will not accept automatically."
                    ),
                    confidence="high",
                    evidence=("runner output explicitly requires legal acceptance",),
                ),
            ),
        )

    offline_resolution_gap = (
        "offline mode" in lowered
        and any(
            marker in lowered
            for marker in (
                "no cached version",
                "has not been downloaded",
                "cannot access",
            )
        )
    ) or (
        re.search(r"could not parse the content at https?://", lowered) is not None
        and any(
            marker in lowered
            for marker in (
                "build is running offline",
                "running with --offline",
            )
        )
    )
    if test_network == "none" and (
        offline_resolution_gap
        or (
            "could not find gem" in lowered
            and "locally installed gems" in lowered
        )
        or any(
            marker in lowered
            for marker in (
                "network is unreachable",
                "temporary failure in name resolution",
                "could not resolve host",
                "unknownhostexception",
                "name or service not known",
                "failed to lookup address information",
                "dns error:",
                "enetunreach",
                "eai_again",
                "getaddrinfo",
            )
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
                    evidence=(
                        "runner output reports a network or offline-resolution failure",
                    ),
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
                suggested_package=_SHARED_LIBRARY_PACKAGE_HINTS.get(name),
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
