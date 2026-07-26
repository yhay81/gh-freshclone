from __future__ import annotations

import configparser
import hashlib
import html
import json
import re
import shlex
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from .configuration import CONFIG_NAME, ConfiguredStep, load_configuration
from .constants import PROFILES
from .model import BaselinePlan, CheckStep, Repository

DEFAULT_PYTHON_MINOR = 13
DEFAULT_NODE_MAJOR = 24
DEFAULT_DOTNET_MAJOR = 10
BOOTSTRAP_UV_VERSION = "0.11.32"
BOOTSTRAP_BUN_VERSION = "1.3.14"
BOOTSTRAP_CMAKE_VERSION = "3.31.10"
BOOTSTRAP_NINJA_VERSION = "1.13.0"
MAVEN_IMAGE = "docker.io/library/maven:3.9-eclipse-temurin-21"
CMAKE_IMAGE = "docker.io/library/python:3.13-bookworm"
COMPOSER_IMAGE = "docker.io/library/composer:2.10.1"
RUBY_IMAGE = "docker.io/library/ruby:3.4.10-bookworm"
SUPPORTED_DOTNET_MAJORS = frozenset({8, 9, 10})
RUBY_RAKE_TASK_PREFIXES = ("tasks/", "lib/tasks/")
_RUBYGEMS_REMOTE = "https://rubygems.org/"
_RUBY_LOCK_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_RUBY_LOCK_SPEC = re.compile(
    r"    ([A-Za-z0-9][A-Za-z0-9_.-]{0,199}) "
    r"\(([A-Za-z0-9][A-Za-z0-9_.-]{0,199})\)"
)
_RUBY_LOCK_CHECKSUM = re.compile(
    r"  ([A-Za-z0-9][A-Za-z0-9_.-]{0,199}) "
    r"\(([A-Za-z0-9][A-Za-z0-9_.-]{0,199})\)"
    r"(?: sha256=([0-9a-f]{64}))?"
)
_CMAKE_TEST_OPTION = re.compile(
    r"(?:^|_)(?:BUILD_?TESTS?|TESTS?)$",
    re.IGNORECASE,
)
_CMAKE_EXPENSIVE_TEST_OPTION = re.compile(
    r"(?:^|_)(?:"
    r"BENCH|CONFORMANCE|CUDA|EXAMPLE|FUZZ|HO|INTEGRATION|PEDANTIC|"
    r"PERFORMANCE|SANITIZE|SYSTEM"
    r")(?:_|$)",
    re.IGNORECASE,
)
_PREPARED_PYTEST_COMMAND = (
    "PATH=/prepared/venv/bin:$PATH "
    "/prepared/venv/bin/python -m pytest -q"
)

_DEPENDENCY_FILES = {
    "python": (
        "uv.lock",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "dev-requirements.txt",
        "test-requirements.txt",
        "setup.cfg",
        "setup.py",
        ".python-version",
    ),
    "node": (
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package.json",
        ".nvmrc",
        ".node-version",
    ),
    "bun": ("bun.lock", "bun.lockb", "package.json"),
    "deno": ("deno.lock", "deno.json", "deno.jsonc"),
    "rust": ("Cargo.lock", "Cargo.toml", "rust-toolchain.toml", "rust-toolchain"),
    "go": ("go.sum", "go.mod"),
    "maven": (
        "pom.xml",
        "mvnw",
        ".mvn/maven.config",
        ".mvn/jvm.config",
        ".mvn/wrapper/maven-wrapper.properties",
    ),
    "gradle": (
        "gradlew",
        "settings.gradle",
        "settings.gradle.kts",
        "build.gradle",
        "build.gradle.kts",
        "gradle.properties",
        "gradle.lockfile",
        "gradle/libs.versions.toml",
        "gradle/wrapper/gradle-wrapper.properties",
    ),
    "cmake": (
        "CMakeLists.txt",
        "CMakePresets.json",
        "CMakeUserPresets.json",
        "vcpkg.json",
        "vcpkg-configuration.json",
        "conanfile.txt",
        "conanfile.py",
    ),
    "dotnet": (
        "global.json",
        "Directory.Build.props",
        "Directory.Build.targets",
        "Directory.Packages.props",
        "NuGet.Config",
        "NuGet.config",
        "nuget.config",
    ),
    "php": (
        "composer.json",
        "composer.lock",
        "phpunit.xml",
        "phpunit.xml.dist",
    ),
    "ruby": (
        "Gemfile",
        "Gemfile.lock",
        ".ruby-version",
        "Rakefile",
        ".rspec",
    ),
}
_ROOT_INPUT_FILES = {
    CONFIG_NAME,
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    *(name for names in _DEPENDENCY_FILES.values() for name in names),
}
AUTOMATIC_PLAN_INPUT_FILES = frozenset(_ROOT_INPUT_FILES)
AUTOMATIC_PLAN_INPUT_SUFFIXES = frozenset({".sln", ".slnx"})
NESTED_MANIFEST_NAMES = frozenset(
    {
        "Cargo.toml",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "CMakeLists.txt",
        "composer.json",
        "Gemfile",
    }
)
_HASH_CHUNK_BYTES = 1024 * 1024


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _reject_escaping_root_inputs(root: Path) -> None:
    resolved_root = root.resolve()
    candidates = [
        *(root / name for name in _ROOT_INPUT_FILES),
        *(path for pattern in ("*.sln", "*.slnx") for path in root.glob(pattern)),
    ]
    validated_solutions: list[Path] = []
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"cannot resolve repository input {path.relative_to(root)}: {exc}"
            ) from exc
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(
                f"repository input escapes the checkout: {path.relative_to(root)}"
            )
        if path.suffix.casefold() in AUTOMATIC_PLAN_INPUT_SUFFIXES:
            validated_solutions.append(path)
    for solution in validated_solutions:
        for value in _dotnet_solution_projects(solution):
            project = root.joinpath(*PurePosixPath(value).parts)
            if not project.exists() and not project.is_symlink():
                continue
            try:
                resolved = project.resolve(strict=True)
            except OSError as exc:
                raise ValueError(
                    f"cannot resolve repository input {value}: {exc}"
                ) from exc
            if not resolved.is_relative_to(resolved_root):
                raise ValueError(f"repository input escapes the checkout: {value}")


def _dependency_fingerprint(
    root: Path,
    ecosystem: str,
    image: str,
    extra_files: tuple[str, ...] = (),
) -> str:
    digest = hashlib.sha256()
    digest.update(b"gh-freshclone-dependencies-v1\0")
    digest.update(ecosystem.encode())
    digest.update(b"\0")
    digest.update(image.encode())
    names = list(_DEPENDENCY_FILES.get(ecosystem, ()))
    if ecosystem == "dotnet":
        names.extend(
            path.name
            for pattern in ("*.sln", "*.slnx")
            for path in sorted(root.glob(pattern), key=lambda item: item.name.casefold())
        )
    names.extend(extra_files)
    for name in dict.fromkeys(names):
        path = root / name
        if not path.is_file():
            continue
        digest.update(b"\0")
        digest.update(name.encode())
        digest.update(b"\0")
        _update_digest_from_file(digest, path)
    return digest.hexdigest()


def _update_digest_from_file(digest: _Digest, path: Path) -> None:
    try:
        with path.open("rb") as source:
            while chunk := source.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError:
        digest.update(b"<unreadable>")


def _configured_dependency_fingerprint(
    root: Path,
    step_root: Path,
    step: ConfiguredStep,
) -> tuple[str, tuple[str, ...]]:
    digest = hashlib.sha256()
    digest.update(b"gh-freshclone-configured-dependencies-v1\0")
    digest.update(step.ecosystem.encode())
    digest.update(b"\0")
    digest.update(step.image.encode())
    candidates = [
        *(PurePosixPath(name) for name in _DEPENDENCY_FILES.get(step.ecosystem, ())),
        *step.dependency_files,
    ]
    evidence = [CONFIG_NAME]
    seen: set[PurePosixPath] = set()
    for relative in candidates:
        if relative in seen:
            continue
        seen.add(relative)
        path = step_root.joinpath(*relative.parts)
        if not path.is_file():
            continue
        repository_relative = path.relative_to(root).as_posix()
        evidence.append(repository_relative)
        digest.update(b"\0")
        digest.update(repository_relative.encode())
        digest.update(b"\0")
        _update_digest_from_file(digest, path)
    config = root / CONFIG_NAME
    digest.update(b"\0")
    digest.update(CONFIG_NAME.encode())
    digest.update(b"\0")
    _update_digest_from_file(digest, config)
    return digest.hexdigest(), tuple(evidence)


def _configured_plan(
    repository: Repository,
    root: Path,
    profile: str,
    configured: tuple[ConfiguredStep, ...],
) -> BaselinePlan:
    steps: list[CheckStep] = []
    for index, configured_step in enumerate(configured):
        if profile not in configured_step.profiles:
            continue
        step_root = root.joinpath(*configured_step.path.parts)
        fingerprint, evidence = _configured_dependency_fingerprint(
            root,
            step_root,
            configured_step,
        )
        steps.append(
            CheckStep(
                ecosystem=configured_step.ecosystem,
                image=configured_step.image,
                command=configured_step.command,
                evidence=(
                    *evidence,
                    f"config.steps[{index}]",
                    f"profile.{profile}",
                ),
                dependency_fingerprint=fingerprint,
                prepare_command=configured_step.prepare_command,
                test_network=configured_step.test_network,
                working_directory=configured_step.path.as_posix(),
            )
        )
    warnings = (
        ()
        if steps
        else (f"{CONFIG_NAME} defines no steps for profile {profile!r}.",)
    )
    return BaselinePlan(
        repository=repository,
        steps=tuple(steps),
        profile=profile,
        warnings=warnings,
    )


def _step(
    root: Path,
    ecosystem: str,
    image: str,
    command: str,
    evidence: tuple[str, ...] | list[str],
    *,
    prepare_command: str = "",
    test_network: str = "none",
    dependency_files: tuple[str, ...] = (),
) -> CheckStep:
    return CheckStep(
        ecosystem=ecosystem,
        image=image,
        command=command,
        evidence=tuple(evidence),
        dependency_fingerprint=_dependency_fingerprint(
            root,
            ecosystem,
            image,
            dependency_files,
        ),
        prepare_command=prepare_command,
        test_network=test_network,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _contains_dependency(dependencies: object, name: str) -> bool:
    if not isinstance(dependencies, list):
        return False
    pattern = re.compile(rf"^{re.escape(name)}(?:$|[-_.\s<>=!~\[])", re.IGNORECASE)
    return any(isinstance(item, str) and pattern.match(item.strip()) for item in dependencies)


def _read_setup_cfg(path: Path) -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as source:
            parser.read_file(source)
    except (OSError, UnicodeDecodeError, configparser.Error):
        return None
    return parser


def _setup_cfg_pytest_extra(
    parser: configparser.ConfigParser | None,
) -> str | None:
    if parser is None or not parser.has_section("options.extras_require"):
        return None
    for name in ("test", "tests", "dev"):
        value = parser.get("options.extras_require", name, fallback="")
        if re.search(
            r"(?im)^\s*pytest(?:$|[-_.\s<>=!~\[])",
            value,
        ):
            return name
    return None


def _python_minor(root: Path, pyproject: dict[str, Any]) -> int:
    version_file = root / ".python-version"
    if version_file.is_file():
        try:
            match = re.search(r"(?m)^\s*3\.(1[1-4])(?:\.\d+)?\s*$", version_file.read_text())
        except (OSError, UnicodeDecodeError):
            match = None
        if match:
            return int(match.group(1))

    project = pyproject.get("project")
    project = project if isinstance(project, dict) else {}
    requirement = str(project.get("requires-python") or "")
    exact = re.search(r"(?:==|~=)\s*3\.(1[1-4])", requirement)
    if exact:
        return int(exact.group(1))
    minimum = re.search(r">=?\s*3\.(1[1-4])", requirement)
    maximum = re.search(r"<\s*3\.(1[2-5])", requirement)
    selected = DEFAULT_PYTHON_MINOR
    if minimum:
        selected = max(selected, int(minimum.group(1)))
    if maximum:
        selected = min(selected, int(maximum.group(1)) - 1)
    return min(14, max(11, selected))


def _expand_tox_envs(value: str) -> list[str]:
    values = [value]
    while any("{" in item and "}" in item for item in values):
        expanded: list[str] = []
        for item in values:
            match = re.search(r"\{([^{}]+)\}", item)
            if not match:
                expanded.append(item)
                continue
            for option in match.group(1).split(","):
                expanded.append(
                    item[: match.start()] + option.strip() + item[match.end() :]
                )
        values = expanded
    return [
        item
        for value in values
        for item in re.split(r"[\s,]+", value)
        if item
    ]


def _tox_env_list(root: Path, tox: dict[str, Any]) -> tuple[str, ...]:
    raw = tox.get("env_list")
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if str(item).strip())
    if isinstance(raw, str):
        return tuple(_expand_tox_envs(raw))

    sources: list[str] = []
    legacy = tox.get("legacy_tox_ini")
    if isinstance(legacy, str):
        sources.append(legacy)
    tox_ini = root / "tox.ini"
    if tox_ini.is_file():
        try:
            sources.append(tox_ini.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass
    for source in sources:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(source)
            env_list = parser.get("tox", "envlist", fallback="")
        except configparser.Error:
            continue
        if env_list:
            return tuple(_expand_tox_envs(env_list))
    return ()


def _tox_python_minor(name: str) -> int | None:
    match = re.fullmatch(r"(?:py)?3[.]?(1[1-4])", name)
    return int(match.group(1)) if match else None


def _quick_tox_environment(
    root: Path,
    tox: dict[str, Any],
    preferred_minor: int,
) -> tuple[str, int]:
    environments = _tox_env_list(root, tox)
    versioned = [
        (name, minor)
        for name in environments
        if (minor := _tox_python_minor(name)) is not None
    ]
    for name, minor in versioned:
        if minor == preferred_minor:
            return name, minor
    if versioned:
        return versioned[0]
    if "py" in environments:
        return "py", preferred_minor
    for name in environments:
        if re.search(r"(?:^|[-_])(test|tests|unit)(?:$|[-_])", name, re.IGNORECASE):
            return name, preferred_minor
    return "py", preferred_minor


def _detect_python(root: Path, profile: str) -> tuple[CheckStep | None, list[str]]:
    warnings: list[str] = []
    pyproject_path = root / "pyproject.toml"
    setup_cfg_path = root / "setup.cfg"
    setup_py_path = root / "setup.py"
    requirements = [
        path
        for name in (
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "dev-requirements.txt",
            "test-requirements.txt",
        )
        if (path := root / name).is_file()
    ]
    has_pytest_config = any(
        (root / name).is_file() for name in ("pytest.ini", "tox.ini", "setup.cfg")
    )
    has_tests = (root / "tests").is_dir() or (root / "test").is_dir()

    if (
        not pyproject_path.is_file()
        and not setup_py_path.is_file()
        and not requirements
    ):
        return None, warnings

    pyproject = _read_toml(pyproject_path) if pyproject_path.is_file() else {}
    setup_cfg = (
        _read_setup_cfg(setup_cfg_path) if setup_cfg_path.is_file() else None
    )
    if not pyproject and setup_cfg is not None:
        requirement = setup_cfg.get("options", "python_requires", fallback="")
        pyproject = {"project": {"requires-python": requirement}}
    minor = _python_minor(root, pyproject)
    evidence: list[str] = []

    if pyproject_path.is_file():
        evidence.append("pyproject.toml")
        project = pyproject.get("project")
        project = project if isinstance(project, dict) else {}
        groups = pyproject.get("dependency-groups")
        groups = groups if isinstance(groups, dict) else {}
        extras = project.get("optional-dependencies")
        extras = extras if isinstance(extras, dict) else {}
        tool = pyproject.get("tool")
        tool = tool if isinstance(tool, dict) else {}
        tox = tool.get("tox")
        tox = tox if isinstance(tox, dict) else {}
        frozen = " --frozen" if (root / "uv.lock").is_file() else ""
        if frozen:
            evidence.append("uv.lock")

        tox_group = next(
            (
                name
                for name in ("dev", "test", "tests")
                if _contains_dependency(groups.get(name), "tox")
            ),
            None,
        )
        has_tox_config = bool(tox) or (root / "tox.ini").is_file()
        selected_group = next(
            (
                name
                for name in ("test", "tests", "dev")
                if _contains_dependency(groups.get(name), "pytest")
            ),
            None,
        )
        selected_extra = next(
            (
                name
                for name in ("test", "tests", "dev")
                if _contains_dependency(extras.get(name), "pytest")
            ),
            None,
        )
        if has_tox_config and tox_group:
            locked = " --locked" if (root / "uv.lock").is_file() else ""
            if profile == "quick":
                tox_environment, minor = _quick_tox_environment(root, tox, minor)
                tox_target = f" -e {shlex.quote(tox_environment)}"
                evidence.append(f"tox.environment.{tox_environment}")
            else:
                tox_target = ""
            image = (
                f"ghcr.io/astral-sh/uv:{BOOTSTRAP_UV_VERSION}-"
                f"python3.{minor}-trixie"
            )
            tox_command = (
                f"uv run{locked} --no-default-groups "
                f"--group {tox_group} tox run{tox_target}"
            )
            prepare_command = f"{tox_command} --notest"
            reuse_flags = " --skip-pkg-install"
            if _contains_dependency(groups.get(tox_group), "tox-uv"):
                reuse_flags += " --skip-uv-sync"
            command = (
                "uv run --offline --no-sync --no-default-groups "
                f"--group {tox_group} tox run{tox_target}{reuse_flags}"
            )
            evidence.extend(
                ("tool.tox", f"dependency-groups.{tox_group}", f"profile.{profile}")
            )
        elif selected_group:
            image = (
                f"ghcr.io/astral-sh/uv:{BOOTSTRAP_UV_VERSION}-"
                f"python3.{minor}-trixie"
            )
            prepare_command = f"uv sync{frozen} --group {selected_group}"
            command = (
                f"uv run --offline --no-sync --group {selected_group} pytest -q"
            )
            evidence.append(f"dependency-groups.{selected_group}")
        elif selected_extra:
            image = (
                f"ghcr.io/astral-sh/uv:{BOOTSTRAP_UV_VERSION}-"
                f"python3.{minor}-trixie"
            )
            prepare_command = f"uv sync{frozen} --extra {selected_extra}"
            command = (
                f"uv run --offline --no-sync --extra {selected_extra} pytest -q"
            )
            evidence.append(f"project.optional-dependencies.{selected_extra}")
        elif _contains_dependency(project.get("dependencies"), "pytest"):
            image = (
                f"ghcr.io/astral-sh/uv:{BOOTSTRAP_UV_VERSION}-"
                f"python3.{minor}-trixie"
            )
            prepare_command = f"uv sync{frozen}"
            command = "uv run --offline --no-sync pytest -q"
            evidence.append("project.dependencies")
        elif has_tests or has_pytest_config:
            image = (
                f"ghcr.io/astral-sh/uv:{BOOTSTRAP_UV_VERSION}-"
                f"python3.{minor}-trixie"
            )
            prepare_command = (
                f"uv sync{frozen} && uv pip install --python .venv pytest"
            )
            command = _PREPARED_PYTEST_COMMAND
            evidence.append("tests/")
            warnings.append(
                "Python tests were found without a declared pytest dependency; "
                "pytest is added ephemerally."
            )
        else:
            return None, warnings
        return (
            _step(
                root,
                "python",
                image,
                command,
                evidence,
                prepare_command=prepare_command,
            ),
            warnings,
        )

    image = (
        f"ghcr.io/astral-sh/uv:{BOOTSTRAP_UV_VERSION}-"
        f"python3.{minor}-trixie"
    )
    requirement_args = " ".join(f"-r {path.name}" for path in requirements)
    if setup_py_path.is_file():
        selected_extra = _setup_cfg_pytest_extra(setup_cfg)
        install_target = (
            shlex.quote(f".[{selected_extra}]") if selected_extra else "."
        )
        install_parts = [install_target]
        if requirement_args:
            install_parts.append(requirement_args)
        if selected_extra is None:
            install_parts.append("pytest")
        install = (
            "uv venv /prepared/venv "
            "&& uv pip install --python /prepared/venv "
            + " ".join(install_parts)
        )
        evidence.append("setup.py")
        if setup_cfg_path.is_file():
            evidence.append("setup.cfg")
        evidence.extend(path.name for path in requirements)
        if selected_extra:
            evidence.append(f"options.extras_require.{selected_extra}")
        else:
            warnings.append(
                "Legacy Python tests were found without a declared pytest "
                "extra; the project and pytest are installed ephemerally."
            )
        if has_tests or has_pytest_config:
            evidence.append("tests/")
            return (
                _step(
                    root,
                    "python",
                    image,
                    _PREPARED_PYTEST_COMMAND,
                    evidence,
                    prepare_command=install,
                ),
                warnings,
            )
        return None, warnings

    evidence.extend(path.name for path in requirements)
    install = (
        "uv venv /prepared/venv "
        "&& uv pip install --python /prepared/venv "
        f"{requirement_args} pytest"
    )
    if has_tests or has_pytest_config:
        command = _PREPARED_PYTEST_COMMAND
        evidence.append("tests/")
        return (
            _step(
                root,
                "python",
                image,
                command,
                evidence,
                prepare_command=install,
            ),
            warnings,
        )
    return None, warnings


def _node_major(root: Path, package: dict[str, Any]) -> int:
    for name in (".nvmrc", ".node-version"):
        path = root / name
        if not path.is_file():
            continue
        try:
            match = re.search(r"(?<!\d)(2[0-9])(?:\.\d+)?", path.read_text())
        except (OSError, UnicodeDecodeError):
            match = None
        if match:
            return int(match.group(1))
    engines = package.get("engines")
    engines = engines if isinstance(engines, dict) else {}
    minimum = re.search(r">=?\s*(2[0-9])", str(engines.get("node") or ""))
    return max(DEFAULT_NODE_MAJOR, int(minimum.group(1))) if minimum else DEFAULT_NODE_MAJOR


def _node_scripts(scripts: object, profile: str) -> tuple[str, ...]:
    if not isinstance(scripts, dict):
        return ()
    selected: list[str] = []
    for name in ("test", "check", "typecheck", "lint", "build"):
        value = scripts.get(name)
        if isinstance(value, str) and value.strip():
            selected.append(name)
            if profile != "full":
                break
    return tuple(selected)


def _node_scripts_with_required_build(
    root: Path,
    package: dict[str, Any],
    profile: str,
) -> tuple[str, ...]:
    scripts = _node_scripts(package.get("scripts"), profile)
    declared = package.get("scripts")
    if (
        "test" not in scripts
        or not isinstance(declared, dict)
        or not isinstance(declared.get("build"), str)
        or not declared["build"].strip()
    ):
        return scripts
    entrypoints: list[str] = []
    for name in ("main", "module", "types", "typings"):
        value = package.get(name)
        if isinstance(value, str):
            entrypoints.append(value)

    def entrypoint_missing(value: str) -> bool:
        relative = PurePosixPath(value.strip().removeprefix("./"))
        if not relative.parts:
            return False
        if relative.is_absolute() or ".." in relative.parts:
            return True
        candidate = root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return True
        return not resolved.is_relative_to(root.resolve()) or not resolved.is_file()

    missing_entrypoint = any(entrypoint_missing(value) for value in entrypoints)
    if not missing_entrypoint:
        return scripts
    return ("build", *(name for name in scripts if name != "build"))


def _detect_deno(root: Path, profile: str) -> CheckStep | None:
    for name in ("deno.json", "deno.jsonc"):
        path = root / name
        if not path.is_file():
            continue
        if name.endswith(".jsonc"):
            return None
        config = _read_json(path)
        tasks = _node_scripts(config.get("tasks"), profile)
        if tasks:
            frozen = " --frozen" if (root / "deno.lock").is_file() else ""
            commands = " && ".join(
                f"deno task{frozen} {task}" for task in tasks
            )
            return _step(
                root,
                "deno",
                "docker.io/denoland/deno:debian",
                commands,
                (name, *(f"tasks.{task}" for task in tasks)),
                prepare_command=f"deno install{frozen}",
            )
    return None


def _detect_node(root: Path, profile: str) -> tuple[CheckStep | None, list[str]]:
    deno = _detect_deno(root, profile)
    if deno:
        return deno, []

    package_path = root / "package.json"
    if not package_path.is_file():
        return None, []
    package = _read_json(package_path)
    scripts = _node_scripts_with_required_build(root, package, profile)
    if not scripts:
        return None, ["package.json has no test, check, typecheck, lint, or build script."]

    manager_field = str(package.get("packageManager") or "")
    evidence = ["package.json", *(f"scripts.{script}" for script in scripts)]
    if manager_field:
        evidence.append("packageManager")

    node_image = f"docker.io/library/node:{_node_major(root, package)}-bookworm"
    if (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
        declared_bun = re.fullmatch(r"bun@(\d+\.\d+\.\d+)", manager_field)
        bun_version = (
            declared_bun.group(1) if declared_bun else BOOTSTRAP_BUN_VERSION
        )
        bun = "/prepared/tools/node_modules/.bin/bun"
        bun_environment = f"PATH=/prepared/tools/node_modules/.bin:$PATH {bun}"
        return (
            _step(
                root,
                "bun",
                node_image,
                " && ".join(
                    f"{bun_environment} run {script}" for script in scripts
                ),
                evidence + ["bun.lock", f"bootstrap.bun@{bun_version}"],
                prepare_command=(
                    "npm install --prefix /prepared/tools --no-audit --no-fund "
                    f"bun@{bun_version} && "
                    f"{bun} install --frozen-lockfile"
                ),
            ),
            [],
        )

    if (root / "pnpm-lock.yaml").is_file() or manager_field.startswith("pnpm@"):
        corepack = "/prepared/corepack/node_modules/.bin/corepack"
        prepare_command = (
            "npm install --prefix /prepared/corepack --no-audit --no-fund "
            f"corepack@0.34.0 && {corepack} pnpm install --frozen-lockfile"
        )
        command = " && ".join(
            f"{corepack} pnpm run {script}" for script in scripts
        )
        return (
            _step(
                root,
                "node",
                node_image,
                command,
                evidence + ["pnpm-lock.yaml"],
                prepare_command=prepare_command,
            ),
            [],
        )
    if (root / "yarn.lock").is_file() or manager_field.startswith("yarn@"):
        corepack = "/prepared/corepack/node_modules/.bin/corepack"
        prepare_command = (
            "npm install --prefix /prepared/corepack --no-audit --no-fund "
            f"corepack@0.34.0 && {corepack} yarn install --immutable"
        )
        command = " && ".join(
            f"{corepack} yarn run {script}" for script in scripts
        )
        return (
            _step(
                root,
                "node",
                node_image,
                command,
                evidence + ["yarn.lock"],
                prepare_command=prepare_command,
            ),
            [],
        )
    if (root / "package-lock.json").is_file():
        prepare_command = "npm ci --no-audit --no-fund"
        evidence.append("package-lock.json")
    else:
        prepare_command = (
            "npm install --no-package-lock --no-audit --no-fund"
        )
    command = " && ".join(f"npm run {script}" for script in scripts)
    return (
        _step(
            root,
            "node",
            node_image,
            command,
            evidence,
            prepare_command=prepare_command,
        ),
        [],
    )


def _detect_rust(root: Path, profile: str) -> tuple[CheckStep | None, list[str]]:
    cargo = root / "Cargo.toml"
    if not cargo.is_file():
        return None, []
    evidence = ["Cargo.toml"]
    warnings: list[str] = []
    channel = ""
    toolchain_toml = root / "rust-toolchain.toml"
    toolchain_plain = root / "rust-toolchain"
    if toolchain_toml.is_file():
        config = _read_toml(toolchain_toml)
        toolchain = config.get("toolchain")
        toolchain = toolchain if isinstance(toolchain, dict) else {}
        channel = str(toolchain.get("channel") or "")
        evidence.append("rust-toolchain.toml")
    elif toolchain_plain.is_file():
        try:
            channel = toolchain_plain.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            channel = ""
        evidence.append("rust-toolchain")

    if re.fullmatch(r"1\.\d+(?:\.\d+)?", channel):
        image = f"docker.io/library/rust:{channel}-bookworm"
    else:
        image = "docker.io/library/rust:bookworm"
        if channel and channel not in {"stable"}:
            warnings.append(
                f"Rust toolchain channel {channel!r} is not mapped to an OCI image; "
                "the stable image will be tried."
            )
    command = (
        "cargo test --offline --workspace --all-features"
        if profile == "full"
        else "cargo test --offline --workspace"
    )
    locked = " --locked" if (root / "Cargo.lock").is_file() else ""
    return (
        _step(
            root,
            "rust",
            image,
            command,
            evidence,
            prepare_command=f"cargo fetch{locked}",
        ),
        warnings,
    )


def _detect_go(root: Path) -> CheckStep | None:
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return None
    try:
        text = go_mod.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    toolchain = re.search(r"(?m)^toolchain\s+go(\d+\.\d+(?:\.\d+)?)", text)
    if toolchain:
        image = f"docker.io/library/golang:{toolchain.group(1)}-bookworm"
        evidence = ("go.mod", "toolchain")
    else:
        # The `go` directive is a minimum language/toolchain requirement, not
        # the repository's preferred current baseline. Use the refreshed
        # stable image unless a toolchain directive opts into an exact release.
        image = "docker.io/library/golang:bookworm"
        evidence = ("go.mod",)
    return _step(
        root,
        "go",
        image,
        "GOPROXY=off go test -count=1 ./...",
        evidence,
        prepare_command="go mod download",
    )


def _detect_maven(root: Path, profile: str) -> CheckStep | None:
    pom = root / "pom.xml"
    if not pom.is_file():
        return None
    wrapper = root / "mvnw"
    executable = "sh ./mvnw" if wrapper.is_file() else "mvn"
    base = f"{executable} -B -ntp -Dmaven.repo.local=/cache/m2"
    fetch = "mvn -B -ntp -Dmaven.repo.local=/cache/m2 dependency:get"
    lifecycle = "verify" if profile == "full" else "test"
    provider_prefetch = (
        "for provider_pom in "
        "/cache/m2/org/apache/maven/plugins/maven-surefire-plugin/*/"
        "maven-surefire-plugin-*.pom; do "
        '[ -f "$provider_pom" ] || continue; '
        'provider_version=$(basename "$(dirname "$provider_pom")"); '
        "for provider in surefire-junit3 surefire-junit4 "
        "surefire-junit47 surefire-junit-platform surefire-testng; do "
        f'{fetch} -Dartifact="org.apache.maven.surefire:'
        '${provider}:${provider_version}" || true; '
        "done; done; "
        "for platform_pom in "
        "/cache/m2/org/junit/platform/junit-platform-engine/*/"
        "junit-platform-engine-*.pom; do "
        '[ -f "$platform_pom" ] || continue; '
        'platform_version=$(basename "$(dirname "$platform_pom")"); '
        f'{fetch} -Dartifact="org.junit.platform:'
        'junit-platform-launcher:${platform_version}" || true; '
        "done"
    )
    evidence = ["pom.xml", f"profile.{profile}"]
    if wrapper.is_file():
        evidence.append("mvnw")
    wrapper_properties = root / ".mvn" / "wrapper" / "maven-wrapper.properties"
    if wrapper_properties.is_file():
        evidence.append(".mvn/wrapper/maven-wrapper.properties")
    return _step(
        root,
        "maven",
        MAVEN_IMAGE,
        f"{base} -o {lifecycle}",
        evidence,
        prepare_command=(
            f"{base} -DskipTests dependency:go-offline && {provider_prefetch}"
        ),
    )


def _detect_gradle(
    root: Path,
    profile: str,
) -> tuple[CheckStep | None, list[str]]:
    manifests = [
        name
        for name in (
            "settings.gradle",
            "settings.gradle.kts",
            "build.gradle",
            "build.gradle.kts",
        )
        if (root / name).is_file()
    ]
    if not manifests:
        return None, []
    wrapper = root / "gradlew"
    if not wrapper.is_file():
        return (
            None,
            [
                (
                    "Gradle build files were found without a committed gradlew "
                    "wrapper; use .gh-freshclone.toml for an explicit baseline."
                )
            ],
        )
    base = "sh ./gradlew --no-daemon --console=plain --max-workers=2"
    lifecycle = "check" if profile == "full" else "test"
    dependency_resolver = (
        "gradle.beforeProject { project -> "
        'project.tasks.register("ghFreshcloneResolveProjectDependencies") { '
        "doLast { project.configurations.findAll { configuration -> "
        "def name = configuration.name.toLowerCase(); "
        "configuration.canBeResolved && name.contains(\"test\") && "
        'name.endsWith("runtimeclasspath") '
        "}.each { configuration -> configuration.resolve() } "
        "} } }; "
        "gradle.projectsEvaluated { def root = gradle.rootProject; "
        'root.tasks.register("ghFreshcloneResolveDependencies") { '
        "dependsOn(root.allprojects.collect { project -> "
        'project.tasks.named("ghFreshcloneResolveProjectDependencies") '
        "}) } }"
    )
    prepare_command = (
        f"printf '%s\\n' {shlex.quote(dependency_resolver)} "
        f"> /tmp/gh-freshclone-resolve.gradle && {base} "
        "--no-configuration-cache "
        "--init-script /tmp/gh-freshclone-resolve.gradle "
        "testClasses ghFreshcloneResolveDependencies"
    )
    evidence = [*manifests, "gradlew", f"profile.{profile}"]
    wrapper_properties = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    java_major = 21
    if wrapper_properties.is_file():
        evidence.append("gradle/wrapper/gradle-wrapper.properties")
        try:
            wrapper_text = wrapper_properties.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            wrapper_text = ""
        match = re.search(
            r"gradle-(\d+)\.(\d+)(?:\.\d+)?"
            r"(?:-[^/\\]+)?-(?:bin|all)\.zip",
            wrapper_text,
        )
        if match:
            gradle_version = int(match.group(1)), int(match.group(2))
            if gradle_version < (8, 5):
                java_major = 17
            evidence.append(
                f"gradle.wrapper.version.{gradle_version[0]}.{gradle_version[1]}"
            )
    declared_toolchain: int | None = None
    for build_name in ("build.gradle", "build.gradle.kts"):
        build_file = root / build_name
        if not build_file.is_file():
            continue
        try:
            build_text = build_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        toolchain_match = re.search(
            r"JavaLanguageVersion\s*\.\s*of\s*\(\s*(\d{1,2})\s*\)",
            build_text,
        )
        if toolchain_match:
            declared_toolchain = int(toolchain_match.group(1))
            break
    gradle_warnings: list[str] = []
    if declared_toolchain in {17, 21, 25}:
        java_major = declared_toolchain
        evidence.append(f"toolchain.java.{declared_toolchain}")
    elif declared_toolchain is not None:
        gradle_warnings.append(
            f"Gradle declares Java toolchain {declared_toolchain}, but the "
            "automatic baseline has no matching multi-architecture runtime; "
            "use .gh-freshclone.toml for an explicit image."
        )
    evidence.append(f"runtime.java.{java_major}")
    return (
        _step(
            root,
            "gradle",
            f"docker.io/library/gradle:jdk{java_major}-noble",
            f"{base} --offline {lifecycle}",
            evidence,
            prepare_command=prepare_command,
        ),
        gradle_warnings,
    )


def _ruby_lock_sections(text: str) -> list[tuple[str, list[str]]] | None:
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            if not re.fullmatch(r"[A-Z][A-Z ]*", line):
                return None
            current = []
            sections.append((line, current))
        elif current is not None:
            current.append(line)
        elif line:
            return None
    return sections


def _ruby_source_specs(lines: list[str]) -> tuple[list[str], set[tuple[str, str]]]:
    remotes: list[str] = []
    specs: set[tuple[str, str]] = set()
    in_specs = False
    for line in lines:
        if line == "  specs:":
            in_specs = True
            continue
        if line.startswith("  remote: "):
            remotes.append(line.removeprefix("  remote: "))
            continue
        if not in_specs:
            continue
        match = _RUBY_LOCK_SPEC.fullmatch(line)
        if match:
            specs.add((match.group(1), match.group(2)))
    return remotes, specs


def _ruby_path_is_contained(root: Path, value: str) -> bool:
    if (
        not value
        or "\\" in value
        or ":" in value
        or "\0" in value
        or any(ord(character) < 32 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return False
    candidate = root.joinpath(*path.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    return resolved.is_dir() and resolved.is_relative_to(root.resolve())


def _ruby_task_signal(root: Path) -> str | None:
    candidates = [root / "Rakefile"]
    for prefix in RUBY_RAKE_TASK_PREFIXES:
        directory = root.joinpath(*PurePosixPath(prefix).parts)
        if directory.is_dir():
            candidates.extend(sorted(directory.rglob("*.rake"))[:128])
    for path in candidates[:129]:
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 256 * 1024:
                continue
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(
            r"\bRake::TestTask\b|"
            r"(?m:^\s*task\s*(?:\(\s*)?(?::test\b|[\"']test[\"']))",
            source,
        ):
            return path.relative_to(root).as_posix()
    return None


def _ruby_download_command(
    downloads: list[tuple[str, str]],
) -> str:
    checksum_lines = [
        shlex.quote(f"{checksum}  {filename}")
        for filename, checksum in downloads
    ]
    return (
        "rm -rf /prepared/gems && mkdir -p /prepared/gems && "
        "cd /prepared/gems && "
        f"printf '%s\\n' {' '.join(checksum_lines)} "
        "> /tmp/gh-freshclone-ruby-sha256 && "
        ": > /tmp/gh-freshclone-ruby-curl && "
        "while read -r checksum filename extra; do "
        'test -n "$checksum" && test -n "$filename" && test -z "$extra" && '
        'case "$filename" in *[!A-Za-z0-9_.-]*|\'\') exit 2;; esac && '
        "printf 'url = \"%s\"\\noutput = \"%s\"\\n' "
        f'"{_RUBYGEMS_REMOTE}downloads/$filename" "$filename" '
        ">> /tmp/gh-freshclone-ruby-curl; "
        "done < /tmp/gh-freshclone-ruby-sha256 && "
        "curl --fail --silent --show-error --location "
        "--proto '=https' --proto-redir '=https' --tlsv1.2 "
        "--retry 2 --retry-all-errors --connect-timeout 30 --max-time 300 "
        "--parallel --parallel-max 8 "
        "--config /tmp/gh-freshclone-ruby-curl && "
        "sha256sum --check --strict /tmp/gh-freshclone-ruby-sha256"
    )


def _detect_ruby(root: Path, profile: str) -> tuple[CheckStep | None, list[str]]:
    gemfile = root / "Gemfile"
    lock_path = root / "Gemfile.lock"
    if not gemfile.is_file():
        return None, []
    if not lock_path.is_file():
        return None, [
            (
                "Gemfile was found without Gemfile.lock; automatic Ruby "
                "execution requires an exact dependency lock."
            )
        ]
    try:
        lock_text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, [
            (
                "Gemfile.lock is not readable UTF-8; use .gh-freshclone.toml "
                "for an explicit Ruby baseline."
            )
        ]
    sections = _ruby_lock_sections(lock_text)
    if sections is None:
        return None, [
            (
                "Gemfile.lock has unsupported syntax; automatic Ruby execution "
                "accepts only a statically parseable Bundler lock."
            )
        ]
    by_name: dict[str, list[list[str]]] = {}
    for name, lines in sections:
        by_name.setdefault(name, []).append(lines)
    prohibited = sorted(name for name in ("GIT", "PLUGIN") if name in by_name)
    if prohibited:
        return None, [
            (
                "Gemfile.lock contains executable or mutable dependency sources "
                f"({', '.join(prohibited)}); automatic networked preparation "
                "accepts only checksummed RubyGems and repository-contained paths."
            )
        ]
    required_singletons = ("DEPENDENCIES", "CHECKSUMS", "PLATFORMS", "BUNDLED WITH")
    if any(len(by_name.get(name, [])) != 1 for name in required_singletons):
        return None, [
            (
                "Gemfile.lock is missing a unique DEPENDENCIES, CHECKSUMS, "
                "PLATFORMS, or BUNDLED WITH section."
            )
        ]

    remote_specs: set[tuple[str, str]] = set()
    for lines in by_name.get("GEM", []):
        remotes, specs = _ruby_source_specs(lines)
        if remotes != [_RUBYGEMS_REMOTE] or not specs:
            return None, [
                (
                    "Gemfile.lock uses a custom or ambiguous gem server; "
                    "automatic acquisition is restricted to https://rubygems.org/."
                )
            ]
        remote_specs.update(specs)
    if not remote_specs:
        return None, [
            (
                "Gemfile.lock contains no checksummed RubyGems dependency graph; "
                "use .gh-freshclone.toml for an explicit baseline."
            )
        ]

    path_specs: set[tuple[str, str]] = set()
    for lines in by_name.get("PATH", []):
        remotes, specs = _ruby_source_specs(lines)
        if (
            len(remotes) != 1
            or not _ruby_path_is_contained(root, remotes[0])
            or not specs
        ):
            return None, [
                (
                    "Gemfile.lock has a PATH source outside the committed "
                    "repository or without a statically identifiable spec."
                )
            ]
        path_specs.update(specs)

    platforms = {
        line.removeprefix("  ")
        for line in by_name["PLATFORMS"][0]
        if line.startswith("  ") and _RUBY_LOCK_TOKEN.fullmatch(line[2:])
    }
    if "ruby" not in platforms:
        return None, [
            (
                "Gemfile.lock has no generic ruby platform; automatic execution "
                "requires a source-gem fallback for both amd64 and arm64."
            )
        ]

    bundled_lines = [
        line[2:]
        for line in by_name["BUNDLED WITH"][0]
        if line.startswith("  ") and line.strip()
    ]
    if len(bundled_lines) != 1 or not _RUBY_LOCK_TOKEN.fullmatch(bundled_lines[0]):
        return None, [
            (
                "Gemfile.lock does not pin one safe BUNDLED WITH version; "
                "refresh it with a current Bundler release."
            )
        ]
    bundler_version = bundled_lines[0]
    bundler_spec = ("bundler", bundler_version)

    checksums: dict[tuple[str, str], str | None] = {}
    for line in by_name["CHECKSUMS"][0]:
        if not line:
            continue
        match = _RUBY_LOCK_CHECKSUM.fullmatch(line)
        if match is None:
            return None, [
                (
                    "Gemfile.lock has an unsupported CHECKSUMS entry; every "
                    "RubyGems archive must have one SHA-256 identity."
                )
            ]
        key = match.group(1), match.group(2)
        if key in checksums:
            return None, [
                "Gemfile.lock contains duplicate CHECKSUMS identities."
            ]
        checksums[key] = match.group(3)

    required_checksums = remote_specs | {bundler_spec}
    if (
        any(checksums.get(spec) is None for spec in required_checksums)
        or any(
            checksum is not None and spec not in required_checksums
            for spec, checksum in checksums.items()
        )
        or any(
            checksum is None and spec not in path_specs
            for spec, checksum in checksums.items()
        )
        or not path_specs.issubset(checksums)
    ):
        return None, [
            (
                "Gemfile.lock does not provide a complete, exact SHA-256 map "
                "for every RubyGems archive and Bundler itself."
            )
        ]

    dependencies: set[str] = set()
    for line in by_name["DEPENDENCIES"][0]:
        match = re.fullmatch(
            r"  ([A-Za-z0-9][A-Za-z0-9_.-]{0,199})(?:!|\s.*)?",
            line,
        )
        if match:
            dependencies.add(match.group(1))
    locked_names = {name for name, _version in remote_specs | path_specs}
    signal = _ruby_task_signal(root)
    runner_name = ""
    runner_command = ""
    runner_evidence = ""
    if (
        signal
        and "rake" in dependencies
        and "rake" in locked_names
        and dependencies.intersection({"minitest", "test-unit"})
        and dependencies.intersection({"minitest", "test-unit"}).issubset(
            locked_names
        )
    ):
        runner_name = min(dependencies.intersection({"minitest", "test-unit"}))
        runner_command = "rake test"
        runner_evidence = signal
    elif (
        (root / "spec").is_dir()
        and dependencies.intersection({"rspec", "rspec-core"})
        and dependencies.intersection({"rspec", "rspec-core"}).issubset(locked_names)
    ):
        runner_name = min(dependencies.intersection({"rspec", "rspec-core"}))
        runner_command = "rspec --format progress"
        runner_evidence = "spec/"
    else:
        return None, [
            (
                "Bundler dependencies do not directly declare a locked ordinary "
                "RSpec suite or a locked Rake-backed Minitest/test-unit task; "
                "automatic execution will not guess a transitive test runner."
            )
        ]

    locked_runner_versions = sorted(
        version for name, version in remote_specs | path_specs if name == runner_name
    )
    if not locked_runner_versions:
        return None, [
            "Gemfile.lock does not contain the directly declared Ruby test runner."
        ]
    downloads = sorted(
        (
            (f"{name}-{version}.gem", checksums[(name, version)] or "")
            for name, version in required_checksums
        ),
        key=lambda item: item[0],
    )
    manifest_size = sum(
        len(filename.encode("utf-8")) + len(checksum) + 4
        for filename, checksum in downloads
    )
    if (
        len(downloads) > 256
        or manifest_size > 24_000
        or any(len(filename.encode("utf-8")) > 240 for filename, _ in downloads)
    ):
        return None, [
            (
                "The checksummed Ruby dependency graph is too large for the "
                "bounded automatic acquisition manifest; use "
                ".gh-freshclone.toml for an explicit baseline."
            )
        ]
    bundler_filename = f"bundler-{bundler_version}.gem"
    command = (
        "rm -rf /tmp/gh-freshclone-gems && "
        "mkdir -p /tmp/gh-freshclone-gems && "
        "gem install --local --no-document --ignore-dependencies "
        "--install-dir /tmp/gh-freshclone-gems "
        f"/prepared/gems/{shlex.quote(bundler_filename)} && "
        "export GEM_HOME=/tmp/gh-freshclone-gems "
        "GEM_PATH=/tmp/gh-freshclone-gems "
        "PATH=/tmp/gh-freshclone-gems/bin:$PATH "
        "BUNDLE_FROZEN=true BUNDLE_DISABLE_VERSION_CHECK=true "
        "BUNDLE_ALLOW_OFFLINE_INSTALL=true BUNDLE_PATH=vendor/bundle "
        "BUNDLE_JOBS=2 && "
        f"bundle _{bundler_version}_ install --local && "
        f"bundle _{bundler_version}_ exec {runner_command}"
    )
    evidence = [
        "Gemfile",
        "Gemfile.lock",
        f"Gemfile.lock:bundler@{bundler_version}",
        f"Gemfile.lock:{runner_name}@{locked_runner_versions[0]}",
        runner_evidence,
        f"profile.{profile}",
    ]
    return (
        _step(
            root,
            "ruby",
            RUBY_IMAGE,
            command,
            evidence,
            prepare_command=_ruby_download_command(downloads),
        ),
        [],
    )


def _detect_php(root: Path, profile: str) -> tuple[CheckStep | None, list[str]]:
    composer_path = root / "composer.json"
    lock_path = root / "composer.lock"
    if not composer_path.is_file():
        return None, []
    if not lock_path.is_file():
        return None, [
            (
                "composer.json was found without composer.lock; automatic PHP "
                "execution requires an exact dependency lock."
            )
        ]

    composer = _read_json(composer_path)
    lock = _read_json(lock_path)
    if not composer or not lock:
        return None, [
            (
                "composer.json or composer.lock is not a readable JSON object; "
                "use .gh-freshclone.toml for an explicit baseline."
            )
        ]
    if not isinstance(lock.get("content-hash"), str):
        return None, [
            (
                "composer.lock has no content-hash; regenerate the lock before "
                "using the automatic PHP baseline."
            )
        ]

    config = composer.get("config")
    config = config if isinstance(config, dict) else {}
    vendor_directory = config.get("vendor-dir", "vendor")
    binary_directory = config.get("bin-dir", "vendor/bin")
    if vendor_directory != "vendor" or binary_directory != "vendor/bin":
        return None, [
            (
                "Composer uses a non-default vendor-dir or bin-dir; use "
                ".gh-freshclone.toml to describe that layout explicitly."
            )
        ]

    direct_section = ""
    for section_name in ("require-dev", "require"):
        section = composer.get(section_name)
        if isinstance(section, dict) and isinstance(
            section.get("phpunit/phpunit"),
            str,
        ):
            direct_section = section_name
            break
    if not direct_section:
        return None, [
            (
                "Composer does not directly declare phpunit/phpunit; automatic "
                "execution will not infer a transitive or globally installed test runner."
            )
        ]

    packages: list[dict[str, Any]] = []
    for section_name in ("packages", "packages-dev"):
        section = lock.get(section_name)
        if isinstance(section, list):
            packages.extend(
                package for package in section if isinstance(package, dict)
            )
    phpunit = next(
        (
            package
            for package in packages
            if package.get("name") == "phpunit/phpunit"
        ),
        None,
    )
    if phpunit is None or not isinstance(phpunit.get("version"), str):
        return None, [
            (
                "composer.lock does not contain the directly declared PHPUnit "
                "package; refresh the lock before automatic execution."
            )
        ]
    plugins = sorted(
        {
            str(package.get("name"))
            for package in packages
            if package.get("type") == "composer-plugin"
            and isinstance(package.get("name"), str)
        }
    )
    if plugins:
        return None, [
            (
                "The locked Composer graph contains executable plugins. Automatic "
                "networked preparation disables plugins, so use .gh-freshclone.toml "
                "only after reviewing the required plugin behavior: "
                + ", ".join(plugins[:5])
            )
        ]

    evidence = [
        "composer.json",
        "composer.lock",
        f"{direct_section}.phpunit/phpunit",
        f"composer.lock:phpunit/phpunit@{phpunit['version']}",
        f"profile.{profile}",
    ]
    for name in ("phpunit.xml", "phpunit.xml.dist"):
        if (root / name).is_file():
            evidence.append(name)
            break
    return (
        _step(
            root,
            "php",
            COMPOSER_IMAGE,
            (
                "COMPOSER_DISABLE_NETWORK=1 "
                "COMPOSER_CACHE_DIR=/tmp/composer-cache "
                "COMPOSER_HOME=/tmp/composer-home composer install "
                "--no-interaction --no-ansi --no-progress "
                "--prefer-dist --no-plugins --no-blocking "
                "&& TERM=dumb COLUMNS=120 vendor/bin/phpunit --colors=never"
            ),
            evidence,
            prepare_command=(
                "composer install --no-interaction --no-ansi --no-progress "
                "--prefer-dist --no-plugins --no-scripts"
            ),
        ),
        [],
    )


def _detect_cmake(
    root: Path,
    profile: str,
) -> tuple[CheckStep | None, list[str]]:
    manifest = root / "CMakeLists.txt"
    if not manifest.is_file():
        return None, []
    try:
        text = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return (
            None,
            [
                (
                    "The root CMakeLists.txt could not be read as UTF-8; use "
                    ".gh-freshclone.toml for an explicit baseline."
                )
            ],
        )
    source = re.sub(
        r"#\[(?P<equals>=*)\[.*?\](?P=equals)\]",
        "",
        text,
        flags=re.DOTALL,
    )

    minimum = re.search(
        r"(?im)^[ \t]*cmake_minimum_required[ \t]*\("
        r"[ \t]*VERSION[ \t]+(\d+(?:\.\d+){0,3})",
        source,
    )
    if minimum:
        required = tuple(int(part) for part in minimum.group(1).split("."))
        available = tuple(int(part) for part in BOOTSTRAP_CMAKE_VERSION.split("."))
        width = max(len(required), len(available))
        if required + (0,) * (width - len(required)) > available + (0,) * (
            width - len(available)
        ):
            return (
                None,
                [
                    (
                        f"CMake requires version {minimum.group(1)}, newer than "
                        f"the automatic {BOOTSTRAP_CMAKE_VERSION} baseline; use "
                        ".gh-freshclone.toml for an explicit image."
                    )
                ],
            )

    test_signal = re.search(
        r"(?im)^[ \t]*(?:"
        r"enable_testing[ \t]*\("
        r"|include[ \t]*\([ \t]*CTest(?:[ \t\)])"
        r"|add_test[ \t]*\()",
        source,
    )
    if not test_signal:
        return (
            None,
            [
                (
                    "The root CMakeLists.txt has no literal CTest signal "
                    "(include(CTest), enable_testing(), or add_test()); use "
                    ".gh-freshclone.toml for an explicit baseline."
                )
            ],
        )

    signal = test_signal.group(0).strip().split("(", maxsplit=1)[0].strip()
    test_options = []
    for option in re.finditer(
        r'(?im)^[ \t]*option[ \t]*\([ \t]*'
        r"([A-Za-z_][A-Za-z0-9_]*)[ \t]+"
        r'"([^"\r\n]*)"',
        source,
    ):
        name, description = option.groups()
        if (
            (name.casefold() == "build_testing" or _CMAKE_TEST_OPTION.search(name))
            and not _CMAKE_EXPENSIVE_TEST_OPTION.search(name)
            and re.search(r"\b(?:build|generate)\b", description, re.IGNORECASE)
            and re.search(r"\btests?\b", description, re.IGNORECASE)
        ):
            test_options.append(name)
    test_option = min(
        set(test_options),
        key=lambda name: (len(name), name.casefold()),
        default=None,
    )
    evidence = [
        "CMakeLists.txt",
        f"ctest.{signal.lower()}",
        f"bootstrap.cmake.{BOOTSTRAP_CMAKE_VERSION}",
        f"bootstrap.ninja.{BOOTSTRAP_NINJA_VERSION}",
        f"profile.{profile}",
    ]
    option_argument = ""
    if test_option:
        evidence.append(f"test-option.{test_option}")
        option_argument = f"-D{test_option}=ON "
    if (root / "CMakePresets.json").is_file():
        evidence.append("CMakePresets.json")
    configure_options = (
        "-G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=ON "
        f"{option_argument}"
        "-DFETCHCONTENT_BASE_DIR=/prepared/fetchcontent"
    )
    prepare_tools = (
        "python -m pip install --disable-pip-version-check --no-input "
        "--no-cache-dir --root-user-action=ignore "
        "--target /prepared/tools "
        f"cmake=={BOOTSTRAP_CMAKE_VERSION} "
        f"ninja=={BOOTSTRAP_NINJA_VERSION}"
    )
    fetchcontent_cleanup = (
        "mkdir -p /prepared/fetchcontent && "
        "find /prepared/fetchcontent -mindepth 1 -maxdepth 1 "
        "-type d \\( -name '*-build' -o -name '*-subbuild' \\) "
        "-exec rm -rf -- {} +"
    )
    return (
        _step(
            root,
            "cmake",
            CMAKE_IMAGE,
            (
                "export PATH=/prepared/tools/bin:$PATH && "
                f"{fetchcontent_cleanup} && "
                "cmake -S . -B .gh-freshclone-build "
                f"{configure_options} "
                "-DFETCHCONTENT_FULLY_DISCONNECTED=ON && "
                "cmake --build .gh-freshclone-build --parallel 2 && "
                "ctest --test-dir .gh-freshclone-build "
                "--parallel 2 --output-on-failure --no-tests=error"
            ),
            evidence,
            prepare_command=(
                f"{prepare_tools} && "
                "export PATH=/prepared/tools/bin:$PATH && "
                "cmake -S . -B /tmp/gh-freshclone-cmake-prepare "
                f"{configure_options} && {fetchcontent_cleanup}"
            ),
        ),
        [],
    )


def _dotnet_solution_projects(path: Path) -> tuple[str, ...]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    values: list[str] = []
    if path.suffix.casefold() == ".slnx":
        values.extend(
            html.unescape(match.group(2))
            for match in re.finditer(
                r"<Project\b[^>]*\bPath\s*=\s*([\"'])(.*?)\1",
                text,
                re.IGNORECASE,
            )
        )
    else:
        values.extend(
            match.group(1)
            for match in re.finditer(
                r'(?im)^Project\("[^"]+"\)\s*=\s*"[^"]+",\s*"([^"]+\.(?:cs|fs|vb)proj)"',
                text,
            )
        )
    projects: list[str] = []
    for value in values:
        candidate = value.strip().replace("\\", "/")
        project = PurePosixPath(candidate)
        if (
            not candidate
            or project.is_absolute()
            or ".." in project.parts
            or project.suffix.casefold() not in {".csproj", ".fsproj", ".vbproj"}
        ):
            continue
        projects.append(project.as_posix())
    return tuple(dict.fromkeys(projects))


def _ordinary_dotnet_test_project(path: str) -> bool:
    stem = PurePosixPath(path).stem.casefold()
    excluded_suffixes = (
        "aottest",
        "benchmark",
        "benchmarks",
        "conformance",
        "example",
        "examples",
        "functional",
        "functionaltests",
        "integration",
        "integrationtests",
        "performance",
        "performancetests",
        "sample",
        "samples",
        "testapp",
        "testdummies",
        "testing",
        "testutils",
    )
    excluded_directories = {
        "bench",
        "benchmark",
        "benchmarks",
        "conformance",
        "examples",
        "integration",
        "performance",
        "samples",
    }
    if stem.endswith(excluded_suffixes) or any(
        part.casefold() in excluded_directories
        for part in PurePosixPath(path).parent.parts
    ):
        return False
    return stem.endswith((".tests", "tests", ".specs", "specs"))


def _dotnet_test_rank(path: str) -> tuple[int, int, str]:
    stem = PurePosixPath(path).stem.casefold()
    if "unittest" in stem or ".core.tests" in stem:
        group = 0
    elif stem.endswith(".tests"):
        group = 1
    elif stem.endswith("tests"):
        group = 2
    else:
        group = 3
    return group, len(path), path.casefold()


def _dotnet_target_framework(path: Path, sdk_major: int) -> str | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    frameworks: list[str] = []
    for match in re.finditer(
        r"<(TargetFrameworks?)\b[^>]*>([^<]*)</\1\s*>",
        text,
        re.IGNORECASE,
    ):
        value_text = match.group(2)
        frameworks.extend(
            value.strip()
            for value in value_text.split(";")
            if value.strip() and "$(" not in value
        )
    target = f"net{sdk_major}.0"
    return target if target in frameworks else None


def _dotnet_image(root: Path) -> tuple[str | None, list[str]]:
    global_json = root / "global.json"
    if not global_json.is_file():
        return f"mcr.microsoft.com/dotnet/sdk:{DEFAULT_DOTNET_MAJOR}.0", []
    config = _read_json(global_json)
    sdk = config.get("sdk")
    sdk = sdk if isinstance(sdk, dict) else {}
    version = sdk.get("version")
    match = (
        re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
        if isinstance(version, str)
        else None
    )
    if match is None:
        return None, [
            (
                "global.json does not declare a stable three-part .NET SDK version; "
                "use .gh-freshclone.toml with an explicit image."
            )
        ]
    major = int(match.group(1))
    if major not in SUPPORTED_DOTNET_MAJORS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_DOTNET_MAJORS))
        return None, [
            (
                f"global.json selects unsupported .NET SDK {version}; automatic "
                f"images cover supported SDK majors {supported}."
            )
        ]
    roll_forward = sdk.get("rollForward")
    tag = f"{major}.0" if isinstance(roll_forward, str) else version
    return f"mcr.microsoft.com/dotnet/sdk:{tag}", []


def _detect_dotnet(
    root: Path,
    profile: str,
) -> tuple[CheckStep | None, list[str]]:
    solutions = sorted(
        (
            *root.glob("*.sln"),
            *root.glob("*.slnx"),
        ),
        key=lambda path: path.name.casefold(),
    )
    if not solutions:
        return None, []
    if len(solutions) != 1:
        names = ", ".join(path.name for path in solutions[:5])
        return None, [
            (
                "Multiple root .NET solutions were found; use .gh-freshclone.toml "
                f"to select one explicitly: {names}"
            )
        ]
    solution = solutions[0]
    projects = _dotnet_solution_projects(solution)
    ordinary_tests = sorted(
        (path for path in projects if _ordinary_dotnet_test_project(path)),
        key=_dotnet_test_rank,
    )
    if not ordinary_tests:
        return None, [
            (
                f"{solution.name} has no statically identifiable ordinary unit-test "
                "project; use .gh-freshclone.toml for an explicit baseline."
            )
        ]
    image, image_warnings = _dotnet_image(root)
    if image is None:
        return None, image_warnings
    image_tag = image.rsplit(":", 1)[-1]
    sdk_major = int(image_tag.split(".", 1)[0])
    target = ordinary_tests[0] if profile == "quick" else solution.name
    quoted_target = shlex.quote(target)
    evidence = [
        solution.name,
        f"solution.project.{target}",
        f"profile.{profile}",
    ]
    if (root / "global.json").is_file():
        evidence.append("global.json")
    dependency_files: tuple[str, ...] = ()
    framework_argument = ""
    if profile == "quick":
        project_file = root.joinpath(*PurePosixPath(target).parts)
        framework = _dotnet_target_framework(project_file, sdk_major)
        if project_file.is_file():
            evidence.append(target)
            dependency_files = (target,)
        if framework:
            evidence.append(f"target-framework.{framework}")
            framework_argument = f" --framework {framework}"
    restore_snapshot = (
        "rm -rf /prepared/restore && mkdir -p /prepared/restore && "
        "find . -type d -name obj -prune "
        "-exec cp --parents -R -- {} /prepared/restore \\;"
    )
    restore_prepared = (
        "find . -type d -name obj -prune -exec rm -rf -- {} + && "
        "cp -R /prepared/restore/. ."
    )
    return (
        _step(
            root,
            "dotnet",
            image,
            (
                f"{restore_prepared} && "
                f"dotnet test {quoted_target} --no-restore "
                f"--configuration Release --nologo{framework_argument}"
            ),
            evidence,
            prepare_command=(
                f"dotnet restore {quoted_target} --disable-parallel --nologo && "
                f"{restore_snapshot}"
            ),
            dependency_files=dependency_files,
        ),
        image_warnings,
    )


def _cargo_workspace_owns(root: Path, manifest: Path) -> bool:
    cargo = _read_toml(root / "Cargo.toml")
    workspace = cargo.get("workspace")
    if not isinstance(workspace, dict):
        return False
    members = workspace.get("members")
    if not isinstance(members, list):
        return False
    excludes = workspace.get("exclude")
    exclude_patterns = excludes if isinstance(excludes, list) else []
    directory = PurePosixPath(manifest.relative_to(root).parent.as_posix())

    def matches(patterns: list[object]) -> bool:
        return any(
            directory.match(item.rstrip("/"))
            for item in patterns
            if isinstance(item, str) and item.rstrip("/")
        )

    return matches(members) and not matches(exclude_patterns)


def detect_plan(
    repository: Repository,
    root: Path,
    profile: str = "quick",
) -> BaselinePlan:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    _reject_escaping_root_inputs(root)
    configured = load_configuration(root)
    if configured is not None:
        return _configured_plan(repository, root, profile, configured)
    steps: list[CheckStep] = []
    warnings: list[str] = []

    rust, rust_warnings = _detect_rust(root, profile)
    node, node_warnings = _detect_node(root, profile)
    python, python_warnings = _detect_python(root, profile)
    go = _detect_go(root)
    maven = _detect_maven(root, profile)
    gradle, gradle_warnings = _detect_gradle(root, profile)
    ruby, ruby_warnings = _detect_ruby(root, profile)
    php, php_warnings = _detect_php(root, profile)
    cmake, cmake_warnings = _detect_cmake(root, profile)
    dotnet, dotnet_warnings = _detect_dotnet(root, profile)
    if maven and gradle and profile != "full":
        if (root / "mvnw").is_file():
            selected_java = "Maven"
            gradle = None
        else:
            selected_java = "Gradle"
            maven = None
        warnings.append(
            f"Both Maven and Gradle baselines were found; profile {profile!r} "
            f"selects {selected_java}, while profile 'full' runs both."
        )
    for step in (
        rust,
        node,
        python,
        go,
        maven,
        gradle,
        ruby,
        php,
        cmake,
        dotnet,
    ):
        if step:
            steps.append(step)
    warnings.extend(rust_warnings)
    warnings.extend(node_warnings)
    warnings.extend(python_warnings)
    warnings.extend(gradle_warnings)
    warnings.extend(ruby_warnings)
    warnings.extend(php_warnings)
    warnings.extend(cmake_warnings)
    warnings.extend(dotnet_warnings)

    if not steps:
        warnings.append(
            "No supported root-level baseline was found. "
            "Automatic support covers Python, Node.js/Bun/Deno, Rust, Go, "
            "Maven, Gradle, Ruby/Bundler, Composer/PHPUnit, CMake, and .NET; "
            "use .gh-freshclone.toml for an explicit layout."
        )
    nested = sorted(
        {
            path.relative_to(root).as_posix()
            for marker in NESTED_MANIFEST_NAMES
            for path in root.glob(f"*/*/{marker}")
            if marker != "Cargo.toml" or not _cargo_workspace_owns(root, path)
        }
    )
    if nested:
        warnings.append(
            "Nested manifests were not auto-run; add explicit "
            ".gh-freshclone.toml steps if they need separate baselines: "
            + ", ".join(nested[:5])
        )
    return BaselinePlan(
        repository=repository,
        steps=tuple(steps),
        profile=profile,
        warnings=tuple(warnings),
    )
