"""Credential-free entrypoint for the gh-freshclone composite GitHub Action."""

from __future__ import annotations

import os
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

# This entrypoint requires one fixed, non-shell argv execution.
_CREDENTIAL_ENV = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
}


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _command(environ: Mapping[str, str]) -> list[str]:
    action_path = _required(environ, "GHFC_ACTION_PATH")
    repository = _required(environ, "GHFC_REPOSITORY")
    no_cache = environ.get("GHFC_NO_CACHE", "false").lower()
    if no_cache not in {"true", "false"}:
        raise ValueError("GHFC_NO_CACHE must be true or false")

    command = [
        "uvx",
        "--from",
        action_path,
        "gh-freshclone",
        "check",
        f"--profile={environ.get('GHFC_PROFILE', 'quick')}",
        f"--component={environ.get('GHFC_COMPONENT', '.')}",
        f"--runner={environ.get('GHFC_RUNNER', 'docker')}",
        f"--test-network={environ.get('GHFC_TEST_NETWORK', 'none')}",
        f"--cpus={environ.get('GHFC_CPUS', '2')}",
        f"--memory={environ.get('GHFC_MEMORY', '4g')}",
        "--json",
    ]
    ref = environ.get("GHFC_REF", "")
    if ref:
        command.append(f"--ref={ref}")
    if no_cache == "true":
        command.append("--no-cache")
    command.extend(("--", repository))
    return command


def _execution_environment(environ: Mapping[str, str]) -> dict[str, str]:
    result = {key: value for key, value in environ.items() if key not in _CREDENTIAL_ENV}
    result.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return result


def run(environ: Mapping[str, str]) -> int:
    if environ.get("RUNNER_OS") != "Linux":
        raise ValueError(
            "the composite action requires a Linux job with Docker or Podman; "
            "use the standalone CLI on macOS or Windows"
        )

    action_path = Path(_required(environ, "GHFC_ACTION_PATH"))
    if not action_path.is_absolute() or not (action_path / "pyproject.toml").is_file():
        raise ValueError("GHFC_ACTION_PATH must be the absolute gh-freshclone action directory")
    runner_temp = Path(_required(environ, "RUNNER_TEMP"))
    github_output = Path(_required(environ, "GITHUB_OUTPUT"))
    if not runner_temp.is_absolute() or not runner_temp.is_dir():
        raise ValueError("RUNNER_TEMP must be an existing absolute directory")

    descriptor, raw_path = tempfile.mkstemp(
        prefix="gh-freshclone-result-",
        suffix=".json",
        dir=runner_temp,
    )
    result_path = Path(raw_path)
    with github_output.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"result-path={result_path}\n")

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as result:
            # Every caller value remains one argv element; shell is never enabled.
            completed = subprocess.run(  # nosec B603
                _command(environ),
                check=False,
                env=_execution_environment(environ),
                stdout=result,
            )
    except OSError as error:
        print(f"failed to start gh-freshclone: {error}", file=sys.stderr)
        return 2

    if result_path.stat().st_size:
        sys.stdout.write(result_path.read_text(encoding="utf-8"))
    return completed.returncode


def main() -> int:
    try:
        return run(os.environ)
    except ValueError as error:
        print(f"gh-freshclone action configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
