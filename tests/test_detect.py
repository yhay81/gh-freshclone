from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gh_freshclone.detect import _dependency_fingerprint, detect_plan
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
    assert plan.steps[0].command == ".venv/bin/python -m pytest -q"
    assert "added ephemerally" in plan.warnings[0]


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
def test_manifest_symlink_cannot_escape_checkout(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-pyproject.toml"
    outside.write_text("[project]\nname='outside'\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").symlink_to(outside)

    with pytest.raises(ValueError, match="repository input escapes"):
        detect_plan(_repository(), tmp_path)
