from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "github_action.py"
SPEC = importlib.util.spec_from_file_location("github_action", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
github_action = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(github_action)


def _environment(tmp_path: Path) -> dict[str, str]:
    action_path = tmp_path / "action"
    action_path.mkdir()
    (action_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    output = tmp_path / "github-output"
    output.touch()
    return {
        "RUNNER_OS": "Linux",
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "GHFC_ACTION_PATH": str(action_path),
        "GHFC_REPOSITORY": "owner/repo",
        "GHFC_REF": "a" * 40,
        "GHFC_PROFILE": "quick",
        "GHFC_COMPONENT": "apps/web",
        "GHFC_RUNNER": "docker",
        "GHFC_TEST_NETWORK": "none",
        "GHFC_NO_CACHE": "true",
        "GHFC_CPUS": "4",
        "GHFC_MEMORY": "8g",
        "GH_TOKEN": "must-not-leak",
        "GITHUB_TOKEN": "must-not-leak",
        "SSH_AUTH_SOCK": "/must/not/leak",
    }


def test_action_passes_inputs_as_arguments_and_scrubs_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _environment(tmp_path)

    def execute(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(json.dumps({"status": "pass"}))
        assert kwargs["check"] is False
        assert "shell" not in kwargs
        child_environment = kwargs["env"]
        assert isinstance(child_environment, dict)
        assert "GH_TOKEN" not in child_environment
        assert "GITHUB_TOKEN" not in child_environment
        assert "SSH_AUTH_SOCK" not in child_environment
        assert child_environment["GIT_TERMINAL_PROMPT"] == "0"
        return subprocess.CompletedProcess(command, 0)

    with mock.patch.object(github_action.subprocess, "run", side_effect=execute) as runner:
        assert github_action.run(environment) == 0

    command = runner.call_args.args[0]
    assert command[:4] == [
        "uvx",
        "--from",
        environment["GHFC_ACTION_PATH"],
        "gh-freshclone",
    ]
    assert "--component=apps/web" in command
    assert f"--ref={'a' * 40}" in command
    assert "--no-cache" in command
    assert command[-2:] == ["--", "owner/repo"]
    assert json.loads(capsys.readouterr().out) == {"status": "pass"}

    output_line = Path(environment["GITHUB_OUTPUT"]).read_text(encoding="utf-8").strip()
    key, result_path = output_line.split("=", 1)
    assert key == "result-path"
    assert Path(result_path).is_file()


def test_action_keeps_untrusted_repository_as_one_post_delimiter_argument(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    environment["GHFC_REPOSITORY"] = "--help; echo unsafe"

    command = github_action._command(environment)

    assert command[-2:] == ["--", "--help; echo unsafe"]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("RUNNER_OS", "Windows", "requires a Linux job"),
        ("GHFC_NO_CACHE", "sometimes", "must be true or false"),
        ("GHFC_REPOSITORY", "", "GHFC_REPOSITORY is required"),
    ],
)
def test_action_rejects_invalid_configuration(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    environment = _environment(tmp_path)
    environment[name] = value

    with pytest.raises(ValueError, match=message):
        github_action.run(environment)


def test_action_preserves_nonzero_result_and_output_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environment = _environment(tmp_path)

    def execute(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(json.dumps({"status": "test_failure"}))
        return subprocess.CompletedProcess(command, 1)

    with mock.patch.object(github_action.subprocess, "run", side_effect=execute):
        assert github_action.run(environment) == 1

    assert json.loads(capsys.readouterr().out) == {"status": "test_failure"}
    output_line = Path(environment["GITHUB_OUTPUT"]).read_text(encoding="utf-8").strip()
    assert Path(output_line.split("=", 1)[1]).is_file()
