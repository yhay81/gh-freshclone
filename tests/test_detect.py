from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from gh_freshclone.detect import (
    BOOTSTRAP_CMAKE_VERSION,
    BOOTSTRAP_NINJA_VERSION,
    CMAKE_IMAGE,
    COMPOSER_IMAGE,
    RUBY_IMAGE,
    _dependency_fingerprint,
    detect_plan,
)
from gh_freshclone.model import Repository


def _repository() -> Repository:
    return Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
    )


def test_dependency_fingerprint_streams_large_lockfiles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    lock = tmp_path / "go.sum"
    lock.write_bytes(b"a" * (2 * 1024 * 1024 + 17))
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: pytest.fail("fingerprinting must stream file content"),
    )

    first = _dependency_fingerprint(tmp_path, "go", "golang:1.24-bookworm")
    with lock.open("r+b") as output:
        output.seek(-1, os.SEEK_END)
        output.write(b"b")
    second = _dependency_fingerprint(tmp_path, "go", "golang:1.24-bookworm")

    assert first != second


def test_detects_python_dependency_group(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sample"
version = "1.0"
requires-python = ">=3.14"

[dependency-groups]
test = ["pytest>=8"]
""",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    plan = detect_plan(_repository(), tmp_path)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.ecosystem == "python"
    assert step.image == "ghcr.io/astral-sh/uv:0.11.32-python3.14-trixie"
    assert step.prepare_command == "uv sync --frozen --group test"
    assert step.command == "uv run --offline --no-sync --group test pytest -q"
    assert step.test_network == "none"
    assert "dependency-groups.test" in step.evidence


def test_detects_python_tests_without_declared_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "1"\n',
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    plan = detect_plan(_repository(), tmp_path)

    assert "uv pip install --python .venv pytest" in plan.steps[0].prepare_command
    assert plan.steps[0].command == (
        "PATH=/prepared/venv/bin:$PATH "
        "/prepared/venv/bin/python -m pytest -q"
    )
    assert "added ephemerally" in plan.warnings[0]


def test_detects_legacy_setup_cfg_test_extra_without_executing_setup_py(
    tmp_path: Path,
) -> None:
    (tmp_path / "setup.py").write_text(
        "raise SystemExit('must not run during planning')\n",
        encoding="utf-8",
    )
    (tmp_path / "setup.cfg").write_text(
        """
[options]
python_requires = >=3.11,<3.13

[options.extras_require]
dev =
    pytest>=8
    pytest-mock
""",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()

    plan = detect_plan(_repository(), tmp_path)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.ecosystem == "python"
    assert step.image.endswith("python3.12-trixie")
    assert step.prepare_command == (
        "uv venv /prepared/venv "
        "&& uv pip install --python /prepared/venv '.[dev]'"
    )
    assert step.command == (
        "PATH=/prepared/venv/bin:$PATH "
        "/prepared/venv/bin/python -m pytest -q"
    )
    assert "setup.py" in step.evidence
    assert "setup.cfg" in step.evidence
    assert "options.extras_require.dev" in step.evidence
    assert plan.warnings == ()


def test_legacy_setup_py_installs_project_and_ephemeral_pytest(
    tmp_path: Path,
) -> None:
    (tmp_path / "setup.py").write_text("", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "dev-requirements.txt").write_text(
        "pytest<8\npytest-relaxed\n",
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    step = plan.steps[0]
    assert step.prepare_command.endswith(
        "uv pip install --python /prepared/venv "
        ". -r dev-requirements.txt pytest"
    )
    assert "dev-requirements.txt" in step.evidence
    assert "without a declared pytest extra" in plan.warnings[0]


def test_legacy_python_dependency_identity_includes_setup_files(
    tmp_path: Path,
) -> None:
    setup_py = tmp_path / "setup.py"
    setup_cfg = tmp_path / "setup.cfg"
    setup_py.write_text("first\n", encoding="utf-8")
    setup_cfg.write_text("[metadata]\nname = sample\n", encoding="utf-8")
    image = "ghcr.io/astral-sh/uv:0.11.32-python3.13-trixie"

    first = _dependency_fingerprint(tmp_path, "python", image)
    setup_py.write_text("second\n", encoding="utf-8")
    second = _dependency_fingerprint(tmp_path, "python", image)
    setup_cfg.write_text("[metadata]\nname = changed\n", encoding="utf-8")
    third = _dependency_fingerprint(tmp_path, "python", image)

    assert first != second
    assert second != third


def test_prefers_declared_tox_frontend(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sample"
version = "1.0"

[dependency-groups]
dev = ["pytest>=8", "tox>=4", "tox-uv"]

[tool.tox]
requires = ["tox>=4"]
""",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    plan = detect_plan(_repository(), tmp_path)

    step = plan.steps[0]
    assert step.prepare_command.endswith("tox run -e py --notest")
    assert step.command.endswith(
        "tox run -e py --skip-pkg-install --skip-uv-sync"
    )
    assert "uv run --offline --no-sync" in step.command
    assert step.image == "ghcr.io/astral-sh/uv:0.11.32-python3.13-trixie"
    assert "tool.tox" in step.evidence
    assert "dependency-groups.dev" in step.evidence
    assert "profile.quick" in step.evidence
    assert "tox.environment.py" in step.evidence

    reproduce = detect_plan(_repository(), tmp_path, "reproduce")

    assert reproduce.steps[0].prepare_command.endswith("tox run --notest")
    assert reproduce.steps[0].command.endswith(
        "tox run --skip-pkg-install --skip-uv-sync"
    )
    assert " -e " not in reproduce.steps[0].command


def test_quick_tox_uses_declared_environment_and_matching_python(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sample"
version = "1.0"

[dependency-groups]
dev = ["tox>=4"]

[tool.tox]
env_list = ["py312", "style"]
""",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.prepare_command.endswith("tox run -e py312 --notest")
    assert step.command.endswith("tox run -e py312 --skip-pkg-install")
    assert step.image.endswith("python3.12-trixie")
    assert "tox.environment.py312" in step.evidence


def test_quick_tox_expands_legacy_factor_environment_list(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sample"
version = "1.0"

[dependency-groups]
test = ["tox>=4"]

[tool.tox]
legacy_tox_ini = '''
[tox]
envlist = py{311,312}, lint
'''
""",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.prepare_command.endswith("tox run -e py311 --notest")
    assert step.command.endswith("tox run -e py311 --skip-pkg-install")
    assert step.image.endswith("python3.11-trixie")


def test_tox_environment_name_is_shell_quoted(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "sample"
version = "1.0"

[dependency-groups]
test = ["tox>=4"]

[tool.tox]
env_list = ["unit-foo; echo unexpected"]
""",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert "-e 'unit-foo; echo unexpected'" in step.prepare_command


def test_detects_npm_script_and_pinned_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "engines": {"node": ">=26"},
                "scripts": {"test": "node --test"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    plan = detect_plan(_repository(), tmp_path)

    step = plan.steps[0]
    assert step.image.endswith("node:26-bookworm")
    assert step.prepare_command == "npm ci --no-audit --no-fund"
    assert step.command == "npm run test"
    assert step.test_network == "none"


def test_bun_baseline_bootstraps_into_a_node_image_for_hybrid_tests(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "bun@1.3.10",
                "scripts": {"test": "bun test && npm --version && node --version"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "bun.lock").write_text("", encoding="utf-8")

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.ecosystem == "bun"
    assert step.image == "docker.io/library/node:24-bookworm"
    assert "bun@1.3.10" in step.prepare_command
    assert step.command == (
        "PATH=/prepared/tools/node_modules/.bin:$PATH "
        "/prepared/tools/node_modules/.bin/bun run test"
    )
    assert "bootstrap.bun@1.3.10" in step.evidence


def test_deno_uses_the_refreshed_official_debian_image(tmp_path: Path) -> None:
    (tmp_path / "deno.json").write_text(
        json.dumps({"tasks": {"test": "deno test"}}),
        encoding="utf-8",
    )
    (tmp_path / "deno.lock").write_text("{}", encoding="utf-8")

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.ecosystem == "deno"
    assert step.image == "docker.io/denoland/deno:debian"
    assert step.prepare_command == "deno install --frozen"
    assert step.command == "deno task --frozen test"


def test_node_builds_a_missing_declared_entrypoint_before_testing(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "main": "./dist/index.js",
                "scripts": {
                    "test": "node --test",
                    "build": "tsc",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.command == "npm run build && npm run test"
    assert "scripts.build" in step.evidence


def test_node_does_not_rebuild_a_committed_entrypoint_for_quick_profile(
    tmp_path: Path,
) -> None:
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "main": "./dist/index.js",
                "scripts": {
                    "test": "node --test",
                    "build": "tsc",
                },
            }
        ),
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.command == "npm run test"


def test_full_node_profile_runs_all_known_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "node --test",
                    "lint": "eslint .",
                    "build": "tsc",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    quick = detect_plan(_repository(), tmp_path, "quick")
    full = detect_plan(_repository(), tmp_path, "full")

    assert quick.steps[0].command.endswith("npm run test")
    assert full.steps[0].command.endswith(
        "npm run test && npm run lint && npm run build"
    )


def test_dependency_fingerprint_changes_with_lockfile(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test"}}),
        encoding="utf-8",
    )
    lock = tmp_path / "package-lock.json"
    lock.write_text('{"lockfileVersion": 3}', encoding="utf-8")
    first = detect_plan(_repository(), tmp_path).steps[0].dependency_fingerprint

    lock.write_text('{"lockfileVersion": 4}', encoding="utf-8")
    second = detect_plan(_repository(), tmp_path).steps[0].dependency_fingerprint

    assert first != second


def test_detects_pnpm_with_corepack(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.13.1",
                "scripts": {"check": "tsc --noEmit"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    plan = detect_plan(_repository(), tmp_path)

    assert "corepack@0.34.0" in plan.steps[0].prepare_command
    assert plan.steps[0].command.endswith("pnpm run check")


def test_detects_rust_and_go_as_independent_steps(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='sample'\nversion='1.0.0'\n")
    (tmp_path / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.88.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text("module example.com/sample\n\ngo 1.24\n")

    plan = detect_plan(_repository(), tmp_path)

    assert [step.ecosystem for step in plan.steps] == ["rust", "go"]
    assert plan.steps[0].image.endswith("rust:1.88.0-bookworm")
    assert plan.steps[0].prepare_command == "cargo fetch"
    assert plan.steps[0].command == "cargo test --offline --workspace"
    assert plan.steps[1].image.endswith("golang:bookworm")
    assert plan.steps[1].prepare_command == "go mod download"
    assert plan.steps[1].command == "GOPROXY=off go test -count=1 ./..."


@pytest.mark.parametrize(
    ("directive", "image"),
    [
        ("1.17", "docker.io/library/golang:bookworm"),
        ("1.18", "docker.io/library/golang:bookworm"),
        ("1.19", "docker.io/library/golang:bookworm"),
        ("1.24", "docker.io/library/golang:bookworm"),
    ],
)
def test_go_image_uses_a_distro_available_for_the_declared_version(
    tmp_path: Path,
    directive: str,
    image: str,
) -> None:
    (tmp_path / "go.mod").write_text(
        f"module example.com/sample\n\ngo {directive}\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.image == image
    assert step.command == "GOPROXY=off go test -count=1 ./..."


def test_go_toolchain_directive_selects_the_preferred_release(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/sample\n\ngo 1.23\ntoolchain go1.24.3\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.image == "docker.io/library/golang:1.24.3-bookworm"
    assert "toolchain" in step.evidence


def test_detects_maven_wrapper_with_offline_test_lifecycle(
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project />\n", encoding="utf-8")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper = tmp_path / ".mvn" / "wrapper"
    wrapper.mkdir(parents=True)
    (wrapper / "maven-wrapper.properties").write_text(
        "distributionUrl=https://repo.maven.apache.org/maven2/\n",
        encoding="utf-8",
    )

    quick = detect_plan(_repository(), tmp_path)
    full = detect_plan(_repository(), tmp_path, "full")

    step = quick.steps[0]
    assert step.ecosystem == "maven"
    assert step.image == "docker.io/library/maven:3.9-eclipse-temurin-21"
    assert step.prepare_command == (
        "sh ./mvnw -B -ntp -Dmaven.repo.local=/cache/m2 "
        "-DskipTests dependency:go-offline && "
        "for provider_pom in "
        "/cache/m2/org/apache/maven/plugins/maven-surefire-plugin/*/"
        "maven-surefire-plugin-*.pom; do "
        '[ -f "$provider_pom" ] || continue; '
        'provider_version=$(basename "$(dirname "$provider_pom")"); '
        "for provider in surefire-junit3 surefire-junit4 "
        "surefire-junit47 surefire-junit-platform surefire-testng; do "
        "mvn -B -ntp -Dmaven.repo.local=/cache/m2 "
        'dependency:get -Dartifact="org.apache.maven.surefire:'
        '${provider}:${provider_version}" || true; '
        "done; done; "
        "for platform_pom in "
        "/cache/m2/org/junit/platform/junit-platform-engine/*/"
        "junit-platform-engine-*.pom; do "
        '[ -f "$platform_pom" ] || continue; '
        'platform_version=$(basename "$(dirname "$platform_pom")"); '
        "mvn -B -ntp -Dmaven.repo.local=/cache/m2 "
        'dependency:get -Dartifact="org.junit.platform:'
        'junit-platform-launcher:${platform_version}" || true; '
        "done"
    )
    assert step.command == (
        "sh ./mvnw -B -ntp -Dmaven.repo.local=/cache/m2 -o test"
    )
    assert "mvnw" in step.evidence
    assert ".mvn/wrapper/maven-wrapper.properties" in step.evidence
    assert "profile.quick" in step.evidence
    assert full.steps[0].command.endswith("-o verify")


def test_maven_without_wrapper_uses_the_pinned_image_tool(
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project />\n", encoding="utf-8")

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.prepare_command.startswith("mvn -B -ntp ")
    assert step.command == "mvn -B -ntp -Dmaven.repo.local=/cache/m2 -o test"
    assert "mvnw" not in step.evidence


def test_detects_only_committed_gradle_wrapper(tmp_path: Path) -> None:
    (tmp_path / "settings.gradle.kts").write_text(
        'rootProject.name = "sample"\n',
        encoding="utf-8",
    )
    (tmp_path / "build.gradle.kts").write_text(
        "plugins { java }\n",
        encoding="utf-8",
    )
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper = tmp_path / "gradle" / "wrapper"
    wrapper.mkdir(parents=True)
    (wrapper / "gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://services.gradle.org/distributions/"
        "gradle-9.1.0-bin.zip\n",
        encoding="utf-8",
    )

    quick = detect_plan(_repository(), tmp_path)
    full = detect_plan(_repository(), tmp_path, "full")

    step = quick.steps[0]
    assert step.ecosystem == "gradle"
    assert step.image == "docker.io/library/gradle:jdk21-noble"
    assert step.prepare_command.startswith(
        "printf '%s\\n' 'gradle.beforeProject {"
    )
    assert "ghFreshcloneResolveProjectDependencies" in step.prepare_command
    assert "configuration.canBeResolved" in step.prepare_command
    assert 'name.contains(\"test\")' in step.prepare_command
    assert 'name.endsWith(\"runtimeclasspath\")' in step.prepare_command
    assert step.prepare_command.endswith(
        "sh ./gradlew --no-daemon --console=plain --max-workers=2 "
        "--no-configuration-cache "
        "--init-script /tmp/gh-freshclone-resolve.gradle "
        "testClasses ghFreshcloneResolveDependencies"
    )
    assert step.command == (
        "sh ./gradlew --no-daemon --console=plain --max-workers=2 "
        "--offline test"
    )
    assert "settings.gradle.kts" in step.evidence
    assert "build.gradle.kts" in step.evidence
    assert "gradle/wrapper/gradle-wrapper.properties" in step.evidence
    assert "gradle.wrapper.version.9.1" in step.evidence
    assert "runtime.java.21" in step.evidence
    assert full.steps[0].command.endswith("--offline check")


@pytest.mark.parametrize(
    ("wrapper_version", "java_major"),
    [
        ("7.6.6", 17),
        ("8.4.3", 17),
        ("8.5.0", 21),
        ("9.0.0", 21),
        ("9.1.0", 21),
        ("9.7.0-rc-1", 21),
    ],
)
def test_gradle_wrapper_selects_a_compatible_jdk(
    tmp_path: Path,
    wrapper_version: str,
    java_major: int,
) -> None:
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n")
    wrapper = tmp_path / "gradle" / "wrapper"
    wrapper.mkdir(parents=True)
    (wrapper / "gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://services.gradle.org/distributions/"
        f"gradle-{wrapper_version}-bin.zip\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.image == (
        f"docker.io/library/gradle:jdk{java_major}-noble"
    )
    assert f"runtime.java.{java_major}" in step.evidence


@pytest.mark.parametrize(
    ("suffix", "syntax", "java_major"),
    [
        ("", "languageVersion = JavaLanguageVersion.of(17)", 17),
        (".kts", "languageVersion.set(JavaLanguageVersion.of(25))", 25),
    ],
)
def test_gradle_declared_toolchain_overrides_wrapper_runtime_default(
    tmp_path: Path,
    suffix: str,
    syntax: str,
    java_major: int,
) -> None:
    (tmp_path / f"build.gradle{suffix}").write_text(
        f"plugins {{ java }}\njava {{ toolchain {{ {syntax} }} }}\n",
        encoding="utf-8",
    )
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper = tmp_path / "gradle" / "wrapper"
    wrapper.mkdir(parents=True)
    (wrapper / "gradle-wrapper.properties").write_text(
        "distributionUrl=https\\://services.gradle.org/distributions/"
        "gradle-9.5.1-bin.zip\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.image == f"docker.io/library/gradle:jdk{java_major}-noble"
    assert f"toolchain.java.{java_major}" in step.evidence
    assert f"runtime.java.{java_major}" in step.evidence


def test_gradle_unsupported_declared_toolchain_requires_configuration(
    tmp_path: Path,
) -> None:
    (tmp_path / "build.gradle").write_text(
        "plugins { id 'java' }\n"
        "java { toolchain { languageVersion = JavaLanguageVersion.of(11) } }\n",
        encoding="utf-8",
    )
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps[0].image == "docker.io/library/gradle:jdk21-noble"
    assert any(
        "declares Java toolchain 11" in warning
        and "explicit image" in warning
        for warning in plan.warnings
    )


def test_gradle_without_wrapper_is_explicitly_not_auto_run(
    tmp_path: Path,
) -> None:
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")

    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps == ()
    assert any("without a committed gradlew wrapper" in item for item in plan.warnings)
    assert any("No supported root-level baseline" in item for item in plan.warnings)


def test_quick_selects_one_alternative_java_build_and_full_runs_both(
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project />\n", encoding="utf-8")
    (tmp_path / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

    quick = detect_plan(_repository(), tmp_path)
    full = detect_plan(_repository(), tmp_path, "full")

    assert [step.ecosystem for step in quick.steps] == ["maven"]
    assert any(
        "selects Maven" in warning and "profile 'full' runs both" in warning
        for warning in quick.warnings
    )
    assert [step.ecosystem for step in full.steps] == ["maven", "gradle"]


def test_quick_prefers_committed_gradle_wrapper_over_image_maven(
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text("<project />\n", encoding="utf-8")
    (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

    plan = detect_plan(_repository(), tmp_path)

    assert [step.ecosystem for step in plan.steps] == ["gradle"]
    assert any("selects Gradle" in warning for warning in plan.warnings)


@pytest.mark.parametrize("ecosystem", ["maven", "gradle"])
def test_java_dependency_identity_includes_wrapper_configuration(
    tmp_path: Path,
    ecosystem: str,
) -> None:
    if ecosystem == "maven":
        (tmp_path / "pom.xml").write_text("<project />\n", encoding="utf-8")
        wrapper = tmp_path / ".mvn" / "wrapper"
        image = "docker.io/library/maven:3.9-eclipse-temurin-21"
    else:
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
        wrapper = tmp_path / "gradle" / "wrapper"
        image = "docker.io/library/eclipse-temurin:21-jdk-noble"
    wrapper.mkdir(parents=True)
    properties = wrapper / (
        "maven-wrapper.properties"
        if ecosystem == "maven"
        else "gradle-wrapper.properties"
    )
    properties.write_text("first\n", encoding="utf-8")

    first = _dependency_fingerprint(tmp_path, ecosystem, image)
    properties.write_text("second\n", encoding="utf-8")
    second = _dependency_fingerprint(tmp_path, ecosystem, image)

    assert first != second


def _write_composer_project(
    root: Path,
    *,
    composer: dict | None = None,
    packages: list[dict] | None = None,
) -> None:
    (root / "composer.json").write_text(
        json.dumps(
            composer
            or {
                "require-dev": {
                    "phpunit/phpunit": "^11.5",
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "composer.lock").write_text(
        json.dumps(
            {
                "content-hash": "locked",
                "packages": [],
                "packages-dev": packages
                if packages is not None
                else [
                    {
                        "name": "phpunit/phpunit",
                        "version": "11.5.42",
                        "type": "library",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_detects_locked_direct_phpunit_without_networked_repository_code(
    tmp_path: Path,
) -> None:
    _write_composer_project(tmp_path)
    (tmp_path / "phpunit.xml.dist").write_text(
        '<phpunit><testsuites><testsuite name="unit" /></testsuites></phpunit>\n',
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.ecosystem == "php"
    assert step.image == COMPOSER_IMAGE
    assert step.test_network == "none"
    assert "composer install" in step.prepare_command
    assert "--no-plugins" in step.prepare_command
    assert "--no-scripts" in step.prepare_command
    assert "composer install" in step.command
    assert step.command.startswith("COMPOSER_DISABLE_NETWORK=1 ")
    assert "COMPOSER_CACHE_DIR=/tmp/composer-cache" in step.command
    assert "COMPOSER_HOME=/tmp/composer-home" in step.command
    assert "--no-blocking" in step.command
    assert "--no-plugins" in step.command
    assert "--no-scripts" not in step.command
    assert "vendor/bin/phpunit --colors=never" in step.command
    assert "composer.lock:phpunit/phpunit@11.5.42" in step.evidence
    assert "phpunit.xml.dist" in step.evidence


def test_php_requires_an_exact_lock_and_direct_locked_phpunit(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text(
        '{"require-dev":{"phpunit/phpunit":"^11.5"}}\n',
        encoding="utf-8",
    )

    unlocked = detect_plan(_repository(), tmp_path)

    assert unlocked.steps == ()
    assert any("without composer.lock" in warning for warning in unlocked.warnings)

    _write_composer_project(
        tmp_path,
        composer={"require-dev": {"mockery/mockery": "^1.6"}},
    )
    indirect = detect_plan(_repository(), tmp_path)

    assert indirect.steps == ()
    assert any("does not directly declare" in warning for warning in indirect.warnings)

    _write_composer_project(tmp_path, packages=[])
    stale = detect_plan(_repository(), tmp_path)

    assert stale.steps == ()
    assert any("does not contain" in warning for warning in stale.warnings)


def test_php_plugin_graph_and_custom_vendor_layout_fail_closed(
    tmp_path: Path,
) -> None:
    _write_composer_project(
        tmp_path,
        packages=[
            {
                "name": "phpunit/phpunit",
                "version": "11.5.42",
                "type": "library",
            },
            {
                "name": "example/installer",
                "version": "1.0.0",
                "type": "composer-plugin",
            },
        ],
    )

    plugin = detect_plan(_repository(), tmp_path)

    assert plugin.steps == ()
    assert any(
        "executable plugins" in warning and "example/installer" in warning
        for warning in plugin.warnings
    )

    _write_composer_project(
        tmp_path,
        composer={
            "require-dev": {"phpunit/phpunit": "^11.5"},
            "config": {"vendor-dir": "build/vendor"},
        },
    )
    custom = detect_plan(_repository(), tmp_path)

    assert custom.steps == ()
    assert any("non-default vendor-dir" in warning for warning in custom.warnings)


def test_php_dependency_identity_includes_lock_and_phpunit_configuration(
    tmp_path: Path,
) -> None:
    _write_composer_project(tmp_path)
    configuration = tmp_path / "phpunit.xml"
    configuration.write_text("<phpunit />\n", encoding="utf-8")

    first = _dependency_fingerprint(tmp_path, "php", COMPOSER_IMAGE)
    configuration.write_text("<phpunit cacheDirectory='.cache' />\n", encoding="utf-8")
    second = _dependency_fingerprint(tmp_path, "php", COMPOSER_IMAGE)
    lock = tmp_path / "composer.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    third = _dependency_fingerprint(tmp_path, "php", COMPOSER_IMAGE)

    assert len({first, second, third}) == 3


def _write_ruby_project(root: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "ruby-bundler"
    shutil.copytree(fixture, root, dirs_exist_ok=True)


def test_detects_checksummed_ruby_bundle_without_networked_repository_code(
    tmp_path: Path,
) -> None:
    _write_ruby_project(tmp_path)

    plan = detect_plan(_repository(), tmp_path)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.ecosystem == "ruby"
    assert step.image == RUBY_IMAGE
    assert step.test_network == "none"
    assert "https://rubygems.org/downloads/$filename" in step.prepare_command
    assert "minitest-5.25.5.gem" in step.prepare_command
    assert "rake-13.2.1.gem" in step.prepare_command
    assert "bundler-4.0.15.gem" in step.prepare_command
    assert "--parallel-max 8" in step.prepare_command
    assert "--config /tmp/gh-freshclone-ruby-curl" in step.prepare_command
    assert "sha256sum --check --strict" in step.prepare_command
    assert "bundle install" not in step.prepare_command
    assert "Gemfile" not in step.prepare_command
    assert "gem install --local" in step.command
    assert "BUNDLE_FROZEN=true" in step.command
    assert "bundle _4.0.15_ install --local" in step.command
    assert "bundle _4.0.15_ exec rake test" in step.command
    assert "Gemfile.lock:minitest@5.25.5" in step.evidence
    assert "Rakefile" in step.evidence


def test_ruby_lock_sources_and_checksums_fail_closed(tmp_path: Path) -> None:
    _write_ruby_project(tmp_path)
    lock = tmp_path / "Gemfile.lock"

    custom_source = lock.read_text(encoding="utf-8").replace(
        "https://rubygems.org/",
        "https://gems.example.invalid/",
    )
    lock.write_text(custom_source, encoding="utf-8")
    custom = detect_plan(_repository(), tmp_path)
    assert custom.steps == ()
    assert any("custom or ambiguous gem server" in item for item in custom.warnings)

    _write_ruby_project(tmp_path)
    missing_checksum = lock.read_text(encoding="utf-8").replace(
        " sha256=391b6c6cb43a4802bfb7c93af1ebe2ac66a210293f4a3fb7db36f2fc7dc2c756",
        "",
    )
    lock.write_text(missing_checksum, encoding="utf-8")
    incomplete = detect_plan(_repository(), tmp_path)
    assert incomplete.steps == ()
    assert any("complete, exact SHA-256 map" in item for item in incomplete.warnings)

    _write_ruby_project(tmp_path)
    lock.write_text(
        "GIT\n  remote: https://github.com/example/code.git\n\n"
        + lock.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    mutable = detect_plan(_repository(), tmp_path)
    assert mutable.steps == ()
    assert any("GIT" in item and "mutable" in item for item in mutable.warnings)


def test_ruby_requires_generic_platform_and_direct_locked_runner(
    tmp_path: Path,
) -> None:
    _write_ruby_project(tmp_path)
    lock = tmp_path / "Gemfile.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8").replace("  ruby\n", "  x86_64-linux\n"),
        encoding="utf-8",
    )
    architecture_specific = detect_plan(_repository(), tmp_path)
    assert architecture_specific.steps == ()
    assert any("generic ruby platform" in item for item in architecture_specific.warnings)

    _write_ruby_project(tmp_path)
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            "  minitest (= 5.25.5)\n",
            "",
        ),
        encoding="utf-8",
    )
    transitive = detect_plan(_repository(), tmp_path)
    assert transitive.steps == ()
    assert any("directly declare" in item for item in transitive.warnings)


def test_ruby_repository_path_must_stay_inside_checkout(tmp_path: Path) -> None:
    _write_ruby_project(tmp_path)
    lock = tmp_path / "Gemfile.lock"
    lock.write_text(
        "PATH\n"
        "  remote: ../outside\n"
        "  specs:\n"
        "    local-gem (1.0.0)\n\n"
        + lock.read_text(encoding="utf-8").replace(
            "  rake (13.2.1) sha256="
            "46cb38dae65d7d74b6020a4ac9d48afed8eb8149c040eccf0523bec91907059d\n",
            "  local-gem (1.0.0)\n"
            "  rake (13.2.1) sha256="
            "46cb38dae65d7d74b6020a4ac9d48afed8eb8149c040eccf0523bec91907059d\n",
        ),
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps == ()
    assert any("PATH source outside" in item for item in plan.warnings)


def test_ruby_dependency_identity_includes_lock_and_rake_evidence(
    tmp_path: Path,
) -> None:
    _write_ruby_project(tmp_path)
    first = _dependency_fingerprint(tmp_path, "ruby", RUBY_IMAGE)
    rakefile = tmp_path / "Rakefile"
    rakefile.write_text(
        rakefile.read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    second = _dependency_fingerprint(tmp_path, "ruby", RUBY_IMAGE)
    lock = tmp_path / "Gemfile.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    third = _dependency_fingerprint(tmp_path, "ruby", RUBY_IMAGE)

    assert len({first, second, third}) == 3


def test_detects_cmake_with_literal_ctest_enablement(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 3.20)
project(FreshcloneBaseline LANGUAGES CXX)
option(FRESHCLONE_BUILD_TESTS "Build tests" OFF)
option(FRESHCLONE_BUILD_TESTS_CUDA "Build tests with CUDA" OFF)
include(CTest)
add_executable(baseline_test test.cpp)
add_test(NAME baseline COMMAND baseline_test)
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "CMakePresets.json").write_text(
        '{"version": 3}\n',
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.ecosystem == "cmake"
    assert step.image == CMAKE_IMAGE
    assert "cmake -S . -B .gh-freshclone-build -G Ninja" in step.command
    assert "-DFETCHCONTENT_BASE_DIR=/prepared/fetchcontent" in step.command
    assert "-DFETCHCONTENT_FULLY_DISCONNECTED=ON" in step.command
    assert "-name '*-build' -o -name '*-subbuild'" in step.command
    assert "-DFRESHCLONE_BUILD_TESTS=ON" in step.command
    assert "FRESHCLONE_BUILD_TESTS_CUDA=ON" not in step.command
    assert "ctest --test-dir .gh-freshclone-build" in step.command
    assert "--no-tests=error" in step.command
    assert step.test_network == "none"
    assert (
        f"cmake=={BOOTSTRAP_CMAKE_VERSION}" in step.prepare_command
    )
    assert (
        f"ninja=={BOOTSTRAP_NINJA_VERSION}" in step.prepare_command
    )
    assert "cmake -S . -B /tmp/gh-freshclone-cmake-prepare" in step.prepare_command
    assert "-DFETCHCONTENT_BASE_DIR=/prepared/fetchcontent" in step.prepare_command
    assert "FETCHCONTENT_FULLY_DISCONNECTED" not in step.prepare_command
    assert "-name '*-build' -o -name '*-subbuild'" in step.prepare_command
    assert f"bootstrap.cmake.{BOOTSTRAP_CMAKE_VERSION}" in step.evidence
    assert f"bootstrap.ninja.{BOOTSTRAP_NINJA_VERSION}" in step.evidence
    assert "test-option.FRESHCLONE_BUILD_TESTS" in step.evidence
    assert "CMakePresets.json" in step.evidence


def test_cmake_without_literal_test_signal_is_not_auto_run(
    tmp_path: Path,
) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 3.20)
project(NoTests)
# include(CTest)
# enable_testing()
#[=[
enable_testing()
]=]
""".lstrip(),
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps == ()
    assert any("no literal CTest" in warning for warning in plan.warnings)
    assert any("No supported root-level baseline" in warning for warning in plan.warnings)


def test_cmake_newer_than_pinned_runtime_requires_configuration(
    tmp_path: Path,
) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 4.0)
project(FutureCMake)
include(CTest)
""".lstrip(),
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps == ()
    assert any(
        "requires version 4.0" in warning
        and BOOTSTRAP_CMAKE_VERSION in warning
        and "explicit image" in warning
        for warning in plan.warnings
    )


@pytest.mark.parametrize(
    ("option_text", "expected"),
    [
        ('option(FMT_TEST "Generate the test target." OFF)', "FMT_TEST"),
        (
            (
                'option(SPDLOG_BUILD_TESTS "Build tests" OFF)\n'
                'option(SPDLOG_BUILD_TESTS_HO "Build tests header-only" OFF)'
            ),
            "SPDLOG_BUILD_TESTS",
        ),
        (
            'option(protobuf_BUILD_TESTS "Build tests" OFF)',
            "protobuf_BUILD_TESTS",
        ),
        (
            'option(FMT_PEDANTIC "Enable extra warnings and expensive tests." OFF)',
            None,
        ),
    ],
)
def test_cmake_selects_only_an_ordinary_project_test_option(
    tmp_path: Path,
    option_text: str,
    expected: str | None,
) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20...4.0)\n"
        f"{option_text}\n"
        "include(CTest)\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    selected = [
        item.removeprefix("test-option.")
        for item in step.evidence
        if item.startswith("test-option.")
    ]
    assert selected == ([] if expected is None else [expected])


def test_cmake_dependency_identity_includes_package_manifests(
    tmp_path: Path,
) -> None:
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\ninclude(CTest)\n",
        encoding="utf-8",
    )
    packages = tmp_path / "vcpkg.json"
    packages.write_text('{"dependencies": ["fmt"]}\n', encoding="utf-8")

    first = _dependency_fingerprint(tmp_path, "cmake", CMAKE_IMAGE)
    packages.write_text('{"dependencies": ["catch2"]}\n', encoding="utf-8")
    second = _dependency_fingerprint(tmp_path, "cmake", CMAKE_IMAGE)

    assert first != second


def test_detects_quick_dotnet_test_from_slnx_without_evaluating_msbuild(
    tmp_path: Path,
) -> None:
    (tmp_path / "global.json").write_text(
        '{"sdk":{"version":"10.0.302","allowPrerelease":false}}\n',
        encoding="utf-8",
    )
    (tmp_path / "Sample.slnx").write_text(
        """
<Solution>
  <!-- <Project Path="test/Commented.Tests/Commented.Tests.csproj" /> -->
  <Project Path="bench/Sample.Benchmarks/Sample.Benchmarks.csproj" />
  <Project Path="src/Sample.Testing/Sample.Testing.csproj" />
  <Project Path="test/Sample.IntegrationTests/Sample.IntegrationTests.csproj" />
  <Project Path="test/Sample.Specs/Sample.Specs.csproj" />
  <Project Path="test/Sample.Core.Tests/Sample.Core.Tests.csproj" />
</Solution>
""".lstrip(),
        encoding="utf-8",
    )
    selected = tmp_path / "test" / "Sample.Core.Tests" / "Sample.Core.Tests.csproj"
    selected.parent.mkdir(parents=True)
    selected.write_text(
        "<Project><PropertyGroup>"
        "<TargetFrameworks>net8.0;net9.0;net10.0</TargetFrameworks>"
        "</PropertyGroup></Project>\n",
        encoding="utf-8",
    )

    quick = detect_plan(_repository(), tmp_path, profile="quick")
    full = detect_plan(_repository(), tmp_path, profile="full")

    assert len(quick.steps) == 1
    step = quick.steps[0]
    assert step.ecosystem == "dotnet"
    assert step.image == "mcr.microsoft.com/dotnet/sdk:10.0.302"
    assert (
        "dotnet restore test/Sample.Core.Tests/Sample.Core.Tests.csproj "
        in step.prepare_command
    )
    assert "find . -type d -name obj" in step.prepare_command
    assert "cp --parents -R" in step.prepare_command
    assert (
        "dotnet test test/Sample.Core.Tests/Sample.Core.Tests.csproj "
        "--no-restore --configuration Release --nologo"
        in step.command
    )
    assert step.command.endswith("--framework net10.0")
    assert "cp -R /prepared/restore/. ." in step.command
    assert "Sample.IntegrationTests" not in step.command
    assert "Sample.Benchmarks" not in step.command
    assert "Commented.Tests" not in step.command
    assert "solution.project.test/Sample.Core.Tests/Sample.Core.Tests.csproj" in (
        step.evidence
    )
    assert "target-framework.net10.0" in step.evidence
    assert full.steps[0].command.endswith(
        "dotnet test Sample.slnx --no-restore --configuration Release --nologo"
    )


def test_dotnet_framework_selection_ignores_only_dynamic_fragments(
    tmp_path: Path,
) -> None:
    (tmp_path / "Product.sln").write_text(
        'Project("{FAKE}") = "Tests", "test\\Product.Tests.csproj", "{A}"\n',
        encoding="utf-8",
    )
    project = tmp_path / "test" / "Product.Tests.csproj"
    project.parent.mkdir()
    project.write_text(
        "<Project><PropertyGroup>"
        "<TargetFrameworks>$(TargetFrameworks);net10.0;net9.0;net8.0"
        "</TargetFrameworks></PropertyGroup></Project>\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.command.endswith("--framework net10.0")


def test_detects_dotnet_solution_and_rolls_forward_declared_sdk(
    tmp_path: Path,
) -> None:
    (tmp_path / "global.json").write_text(
        '{"sdk":{"version":"10.0.100","rollForward":"latestFeature"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "Product.sln").write_text(
        '\ufeffMicrosoft Visual Studio Solution File, Format Version 12.00\n'
        'Project("{FAKE}") = "Product", "src\\Product\\Product.csproj", "{A}"\n'
        "EndProject\n"
        'Project("{FAKE}") = "UnitTests", '
        '"src\\UnitTests\\Product.UnitTests.csproj", "{B}"\n'
        "EndProject\n",
        encoding="utf-8",
    )

    step = detect_plan(_repository(), tmp_path).steps[0]

    assert step.image == "mcr.microsoft.com/dotnet/sdk:10.0"
    assert "src/UnitTests/Product.UnitTests.csproj" in step.command


def test_dotnet_ambiguous_solution_and_unsupported_sdk_fail_closed(
    tmp_path: Path,
) -> None:
    for name in ("First.sln", "Second.slnx"):
        (tmp_path / name).write_text("<Solution />\n", encoding="utf-8")

    ambiguous = detect_plan(_repository(), tmp_path)

    assert ambiguous.steps == ()
    assert any("Multiple root .NET solutions" in item for item in ambiguous.warnings)

    (tmp_path / "Second.slnx").unlink()
    (tmp_path / "First.sln").write_text(
        'Project("{FAKE}") = "Tests", "Tests\\Product.Tests.csproj", "{A}"\n',
        encoding="utf-8",
    )
    (tmp_path / "global.json").write_text(
        '{"sdk":{"version":"7.0.410"}}\n',
        encoding="utf-8",
    )

    unsupported = detect_plan(_repository(), tmp_path)

    assert unsupported.steps == ()
    assert any("unsupported .NET SDK 7.0.410" in item for item in unsupported.warnings)


def test_dotnet_solution_changes_dependency_identity(tmp_path: Path) -> None:
    solution = tmp_path / "Product.sln"
    solution.write_text("first\n", encoding="utf-8")
    image = "mcr.microsoft.com/dotnet/sdk:10.0"

    first = _dependency_fingerprint(tmp_path, "dotnet", image)
    solution.write_text("second\n", encoding="utf-8")
    second = _dependency_fingerprint(tmp_path, "dotnet", image)

    assert first != second


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not generally available")
@pytest.mark.parametrize("escape", ["solution", "project"])
def test_dotnet_inputs_cannot_escape_checkout(
    tmp_path: Path,
    escape: str,
) -> None:
    outside = tmp_path.parent / f"outside-{escape}.xml"
    outside.write_text(
        '<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework>'
        "</PropertyGroup></Project>\n",
        encoding="utf-8",
    )
    solution = tmp_path / "Product.slnx"
    if escape == "solution":
        solution.symlink_to(outside)
    else:
        solution.write_text(
            '<Solution><Project Path="test/Product.Tests.csproj" /></Solution>\n',
            encoding="utf-8",
        )
        project = tmp_path / "test" / "Product.Tests.csproj"
        project.parent.mkdir()
        project.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes the checkout"):
        detect_plan(_repository(), tmp_path)


def test_cargo_workspace_members_are_covered_by_the_root_step(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        """
[workspace]
members = ["crates/*"]
exclude = ["crates/excluded"]
""",
        encoding="utf-8",
    )
    for name in ("member", "excluded"):
        crate = tmp_path / "crates" / name
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text(
            f"[package]\nname = {name!r}\nversion = '1.0.0'\n",
            encoding="utf-8",
        )

    plan = detect_plan(_repository(), tmp_path)

    assert not any("crates/member/Cargo.toml" in warning for warning in plan.warnings)
    assert any("crates/excluded/Cargo.toml" in warning for warning in plan.warnings)
    assert any("were not auto-run" in warning for warning in plan.warnings)


def test_no_supported_manifest_is_explicit(tmp_path: Path) -> None:
    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps == ()
    assert "No supported root-level baseline" in plan.warnings[0]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not generally available")
@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("pyproject.toml", "[project]\nname='outside'\n"),
        ("setup.py", "raise SystemExit('must not execute')\n"),
        ("pom.xml", "<project />\n"),
        ("gradlew", "#!/bin/sh\n"),
    ],
)
def test_manifest_symlink_cannot_escape_checkout(
    tmp_path: Path,
    filename: str,
    contents: str,
) -> None:
    outside = tmp_path.parent / f"outside-{filename}"
    outside.write_text(contents, encoding="utf-8")
    (tmp_path / filename).symlink_to(outside)

    with pytest.raises(ValueError, match="repository input escapes"):
        detect_plan(_repository(), tmp_path)
