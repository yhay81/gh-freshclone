from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .constants import PROFILES

CONFIG_NAME = ".gh-freshclone.toml"
CONFIG_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
MAX_STEPS = 32
MAX_COMMAND_LENGTH = 4096
MAX_DEPENDENCY_FILES = 32

_STEP_KEYS = {
    "profiles",
    "path",
    "ecosystem",
    "image",
    "command",
    "prepare_command",
    "test_network",
    "dependency_files",
}
_ECOSYSTEM = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_IMAGE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]*"
    r"(?:@[A-Za-z0-9_+.-]+:[A-Fa-f0-9]+)?$"
)


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfiguredStep:
    profiles: tuple[str, ...]
    path: PurePosixPath
    ecosystem: str
    image: str
    command: str
    prepare_command: str
    test_network: str
    dependency_files: tuple[PurePosixPath, ...]


def _portable_relative_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} must be a non-empty POSIX path")
    if "\\" in value:
        raise ConfigurationError(f"{field} must use '/' separators")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"{field} must stay within the repository")
    return path


def _profiles(value: object, index: int) -> tuple[str, ...]:
    if value is None:
        return PROFILES
    if not isinstance(value, list) or not value:
        raise ConfigurationError(f"steps[{index}].profiles must be a non-empty array")
    profiles = tuple(str(item) for item in value)
    unknown = sorted(set(profiles) - set(PROFILES))
    if unknown:
        raise ConfigurationError(
            f"steps[{index}].profiles contains unknown values: {', '.join(unknown)}"
        )
    if len(set(profiles)) != len(profiles):
        raise ConfigurationError(f"steps[{index}].profiles contains duplicates")
    return profiles


def _configured_step(
    raw: object,
    *,
    index: int,
    root: Path,
) -> ConfiguredStep:
    if not isinstance(raw, dict):
        raise ConfigurationError(f"steps[{index}] must be a table")
    unknown = sorted(set(raw) - _STEP_KEYS)
    if unknown:
        raise ConfigurationError(
            f"steps[{index}] contains unknown keys: {', '.join(unknown)}"
        )

    step_path = _portable_relative_path(raw.get("path", "."), f"steps[{index}].path")
    resolved = root.joinpath(*step_path.parts).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_dir():
        raise ConfigurationError(
            f"steps[{index}].path is not an existing repository directory"
        )

    ecosystem = raw.get("ecosystem")
    if not isinstance(ecosystem, str) or not _ECOSYSTEM.fullmatch(ecosystem):
        raise ConfigurationError(
            f"steps[{index}].ecosystem must match {_ECOSYSTEM.pattern}"
        )
    image = raw.get("image")
    if (
        not isinstance(image, str)
        or not image
        or len(image) > 512
        or any(character.isspace() for character in image)
        or "\0" in image
        or not _IMAGE.fullmatch(image)
    ):
        raise ConfigurationError(
            f"steps[{index}].image must be a safe OCI reference"
        )
    command = raw.get("command")
    if (
        not isinstance(command, str)
        or not command.strip()
        or len(command) > MAX_COMMAND_LENGTH
        or "\0" in command
    ):
        raise ConfigurationError(
            f"steps[{index}].command must contain 1-{MAX_COMMAND_LENGTH} characters"
        )
    prepare_command = raw.get("prepare_command", "")
    if (
        not isinstance(prepare_command, str)
        or len(prepare_command) > MAX_COMMAND_LENGTH
        or "\0" in prepare_command
    ):
        raise ConfigurationError(
            f"steps[{index}].prepare_command must contain at most "
            f"{MAX_COMMAND_LENGTH} characters"
        )
    test_network = raw.get(
        "test_network",
        "none" if prepare_command.strip() else "enabled",
    )
    if test_network not in {"none", "enabled"}:
        raise ConfigurationError(
            f"steps[{index}].test_network must be 'none' or 'enabled'"
        )
    if test_network == "none" and not prepare_command.strip():
        raise ConfigurationError(
            f"steps[{index}] needs prepare_command when test_network is 'none'"
        )

    raw_dependencies = raw.get("dependency_files", [])
    if not isinstance(raw_dependencies, list):
        raise ConfigurationError(
            f"steps[{index}].dependency_files must be an array"
        )
    if len(raw_dependencies) > MAX_DEPENDENCY_FILES:
        raise ConfigurationError(
            f"steps[{index}].dependency_files exceeds {MAX_DEPENDENCY_FILES}"
        )
    dependency_files: list[PurePosixPath] = []
    for dependency_index, value in enumerate(raw_dependencies):
        dependency = _portable_relative_path(
            value,
            f"steps[{index}].dependency_files[{dependency_index}]",
        )
        dependency_path = resolved.joinpath(*dependency.parts).resolve()
        if (
            not dependency_path.is_relative_to(resolved)
            or not dependency_path.is_file()
        ):
            raise ConfigurationError(
                f"steps[{index}].dependency_files[{dependency_index}] "
                "is not an existing file below the step path"
            )
        dependency_files.append(dependency)

    return ConfiguredStep(
        profiles=_profiles(raw.get("profiles"), index),
        path=step_path,
        ecosystem=ecosystem,
        image=image,
        command=command.strip(),
        prepare_command=prepare_command.strip(),
        test_network=str(test_network),
        dependency_files=tuple(dependency_files),
    )


def load_configuration(root: Path) -> tuple[ConfiguredStep, ...] | None:
    path = root / CONFIG_NAME
    if not path.is_file():
        return None
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            raise ConfigurationError(f"{CONFIG_NAME} must stay within the repository")
    except OSError as exc:
        raise ConfigurationError(f"cannot resolve {CONFIG_NAME}: {exc}") from exc
    try:
        size = path.stat().st_size
        if size > MAX_CONFIG_BYTES:
            raise ConfigurationError(
                f"{CONFIG_NAME} exceeds the {MAX_CONFIG_BYTES}-byte limit"
            )
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot parse {CONFIG_NAME}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{CONFIG_NAME} must contain a TOML table")
    unknown = sorted(set(value) - {"version", "steps"})
    if unknown:
        raise ConfigurationError(
            f"{CONFIG_NAME} contains unknown keys: {', '.join(unknown)}"
        )
    if value.get("version") != CONFIG_VERSION:
        raise ConfigurationError(
            f"{CONFIG_NAME} version must be {CONFIG_VERSION}"
        )
    raw_steps = value.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ConfigurationError(f"{CONFIG_NAME} must define at least one [[steps]]")
    if len(raw_steps) > MAX_STEPS:
        raise ConfigurationError(f"{CONFIG_NAME} exceeds {MAX_STEPS} steps")
    return tuple(
        _configured_step(raw, index=index, root=root)
        for index, raw in enumerate(raw_steps)
    )
