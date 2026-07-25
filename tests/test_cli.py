from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from gh_freshclone import cli
from gh_freshclone.cache import CacheReport
from gh_freshclone.model import BaselinePlan, Repository


def test_json_output_is_ascii_safe_on_legacy_windows_console(
    git_repository: Path,
) -> None:
    (git_repository / ".gh-freshclone.toml").write_text(
        """
version = 1
[[steps]]
ecosystem = "custom"
image = "docker.io/library/alpine:3"
command = "printf '▶'"
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "add", ".gh-freshclone.toml"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repository),
            "-c",
            "user.name=Freshclone Tests",
            "-c",
            "user.email=freshclone@example.invalid",
            "commit",
            "-m",
            "add config",
        ],
        check=True,
        capture_output=True,
    )
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp932"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gh_freshclone.cli",
            "plan",
            str(git_repository),
            "--json",
        ],
        env=environment,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("ascii", errors="replace")
    completed.stdout.decode("ascii")
    assert b"\\u25b6" in completed.stdout


def test_public_exit_codes_for_empty_plan_and_initialization_error(
    monkeypatch,
    capsys,
) -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url=None,
        github_repository="owner/repo",
        local_path=None,
    )
    monkeypatch.setattr(
        cli,
        "create_plan",
        lambda *args, **kwargs: BaselinePlan(repository=repository, steps=()),
    )

    assert cli.main(["plan", "owner/repo"]) == 1
    capsys.readouterr()

    def fail(*args, **kwargs):
        raise ValueError("invalid configuration")

    monkeypatch.setattr(cli, "create_plan", fail)

    assert cli.main(["plan", "owner/repo"]) == 2


def test_doctor_requires_git_and_a_ready_runner_but_not_github_auth(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_which", lambda command: f"/tools/{command}")
    monkeypatch.setattr(cli, "available_runners", lambda: {"docker": "29.0"})
    monkeypatch.setattr(cli, "runner_ready", lambda name: True)

    assert cli.main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["github"] == {
        "mode": "credential-free-https",
        "authentication_required": False,
    }
    assert payload["runners"]["docker"]["ready"] is True


def test_invalid_resource_limit_is_an_initialization_error(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "probe_repository",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("cpus must be a finite number greater than zero")
        ),
    )

    assert cli.main(["check", "owner/repo", "--cpus", "nan"]) == 2
    assert "finite number greater than zero" in capsys.readouterr().err


def test_json_check_initialization_error_remains_machine_readable(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "probe_repository",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("image tag is unavailable")
        ),
    )

    assert cli.main(["check", "owner/repo", "--json"]) == 2

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "api_version": 1,
        "status": "error",
        "error": {
            "code": "initialization_error",
            "message": "image tag is unavailable",
        },
    }


def test_cache_prune_exposes_evidence_limits(
    monkeypatch,
    capsys,
) -> None:
    observed: dict[str, object] = {}

    def fake_prune(**kwargs):
        observed.update(kwargs)
        return CacheReport(host_bytes=0, host_entries=0, prepared_volumes=0)

    monkeypatch.setattr(cli, "prune_cache", fake_prune)

    assert (
        cli.main(
            [
                "cache",
                "prune",
                "--max-evidence-gib",
                "0.5",
                "--max-evidence-entries",
                "256",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert observed["max_evidence_bytes"] == int(0.5 * 1024**3)
    assert observed["max_evidence_entries"] == 256


def test_doctor_rejects_legacy_apple_container(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_which", lambda command: f"/tools/{command}")
    monkeypatch.setattr(
        cli,
        "available_runners",
        lambda: {"container": "container 0.12.3"},
    )
    monkeypatch.setattr(
        cli,
        "runner_ready",
        lambda name: (_ for _ in ()).throw(
            AssertionError("unsupported runner readiness must not be probed")
        ),
    )

    assert cli.main(["doctor", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["runners"]["container"] == {
        "version": "container 0.12.3",
        "supported": False,
        "ready": False,
        "minimum_version": "1.0.0",
    }
