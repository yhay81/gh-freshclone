from __future__ import annotations

import configparser
import hashlib
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
BOOTSTRAP_UV_VERSION = "0.11.32"
BOOTSTRAP_BUN_VERSION = "1.3.14"
MAVEN_IMAGE = "docker.io/library/maven:3.9-eclipse-temurin-21"
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
}
_ROOT_INPUT_FILES = {
    CONFIG_NAME,
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    *(name for names in _DEPENDENCY_FILES.values() for name in names),
}
AUTOMATIC_PLAN_INPUT_FILES = frozenset(_ROOT_INPUT_FILES)
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
    }
)
_HASH_CHUNK_BYTES = 1024 * 1024


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _reject_escaping_root_inputs(root: Path) -> None:
    resolved_root = root.resolve()
    for name in _ROOT_INPUT_FILES:
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"cannot resolve repository input {name}: {exc}") from exc
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(f"repository input escapes the checkout: {name}")


def _dependency_fingerprint(root: Path, ecosystem: str, image: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"gh-freshclone-dependencies-v1\0")
    digest.update(ecosystem.encode())
    digest.update(b"\0")
    digest.update(image.encode())
    for name in _DEPENDENCY_FILES.get(ecosystem, ()):
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
) -> CheckStep:
    return CheckStep(
        ecosystem=ecosystem,
        image=image,
        command=command,
        evidence=tuple(evidence),
        dependency_fingerprint=_dependency_fingerprint(root, ecosystem, image),
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
    for step in (rust, node, python, go, maven, gradle):
        if step:
            steps.append(step)
    warnings.extend(rust_warnings)
    warnings.extend(node_warnings)
    warnings.extend(python_warnings)
    warnings.extend(gradle_warnings)

    if not steps:
        warnings.append(
            "No supported root-level baseline was found. "
            "Automatic support covers Python, Node.js/Bun/Deno, Rust, Go, "
            "Maven, and Gradle; "
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
