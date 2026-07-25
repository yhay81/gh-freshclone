from __future__ import annotations

from pathlib import Path

import pytest

from gh_freshclone.configuration import ConfigurationError
from gh_freshclone.detect import detect_plan
from gh_freshclone.model import Repository


def _repository() -> Repository:
    return Repository(
        display_name="owner/monorepo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/monorepo",
        github_repository="owner/monorepo",
        local_path=None,
    )


def test_explicit_config_compiles_profiled_monorepo_steps(tmp_path: Path) -> None:
    api = tmp_path / "services" / "api"
    web = tmp_path / "apps" / "web"
    api.mkdir(parents=True)
    web.mkdir(parents=True)
    (api / "pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
    (api / "uv.lock").write_text("lock", encoding="utf-8")
    (web / "package.json").write_text('{"name":"web"}', encoding="utf-8")
    (web / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".gh-freshclone.toml").write_text(
        """
version = 1

[[steps]]
profiles = ["quick", "reproduce"]
path = "services/api"
ecosystem = "python"
image = "ghcr.io/astral-sh/uv:0.11.32-python3.13-trixie"
prepare_command = "uv sync --frozen --group test"
command = "uv run --offline --no-sync --group test pytest -q"
dependency_files = ["uv.lock"]

[[steps]]
profiles = ["full"]
path = "apps/web"
ecosystem = "node"
image = "docker.io/library/node:24-bookworm"
prepare_command = "npm ci"
command = "npm test"
""",
        encoding="utf-8",
    )

    quick = detect_plan(_repository(), tmp_path, "quick")
    full = detect_plan(_repository(), tmp_path, "full")

    assert [step.ecosystem for step in quick.steps] == ["python"]
    assert quick.steps[0].working_directory == "services/api"
    assert quick.steps[0].prepare_command == "uv sync --frozen --group test"
    assert quick.steps[0].command == "uv run --offline --no-sync --group test pytest -q"
    assert quick.steps[0].test_network == "none"
    assert "services/api/uv.lock" in quick.steps[0].evidence
    assert [step.ecosystem for step in full.steps] == ["node"]
    assert full.steps[0].working_directory == "apps/web"
    assert full.steps[0].prepare_command == "npm ci"
    assert full.steps[0].command == "npm test"
    assert quick.steps[0].dependency_fingerprint != full.steps[0].dependency_fingerprint


def test_config_is_authoritative_over_root_auto_detection(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}',
        encoding="utf-8",
    )
    (tmp_path / ".gh-freshclone.toml").write_text(
        """
version = 1
[[steps]]
ecosystem = "custom"
image = "docker.io/library/alpine:3"
command = "true"
""",
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert len(plan.steps) == 1
    assert plan.steps[0].ecosystem == "custom"
    assert plan.steps[0].command == "true"
    assert plan.steps[0].prepare_command == ""
    assert plan.steps[0].test_network == "none"


def test_config_must_explicitly_request_test_network(tmp_path: Path) -> None:
    (tmp_path / ".gh-freshclone.toml").write_text(
        """
version = 1
[[steps]]
ecosystem = "custom"
image = "docker.io/library/alpine:3"
command = "true"
test_network = "enabled"
""",
        encoding="utf-8",
    )

    plan = detect_plan(_repository(), tmp_path)

    assert plan.steps[0].test_network == "enabled"


@pytest.mark.parametrize(
    "config, message",
    [
        (
            """
version = 1
[[steps]]
path = "../outside"
ecosystem = "python"
image = "python:3.13"
command = "pytest"
""",
            "must stay within the repository",
        ),
        (
            """
version = 1
[[steps]]
profiles = ["fast"]
ecosystem = "python"
image = "python:3.13"
command = "pytest"
""",
            "unknown values: fast",
        ),
        (
            """
version = 1
[[steps]]
ecosystem = "python"
image = "python:3.13"
command = "pytest"
typo = true
""",
            "unknown keys: typo",
        ),
        (
            """
version = 1
[[steps]]
ecosystem = "custom"
image = "--privileged"
command = "true"
""",
            "safe OCI reference",
        ),
    ],
)
def test_invalid_config_fails_closed(
    tmp_path: Path,
    config: str,
    message: str,
) -> None:
    (tmp_path / ".gh-freshclone.toml").write_text(config, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        detect_plan(_repository(), tmp_path)
