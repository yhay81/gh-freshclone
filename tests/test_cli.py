from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from gh_freshclone import cli
from gh_freshclone.api import ProbeOutcome
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


def test_test_network_policy_is_forwarded_by_plan_and_check(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url=None,
        github_repository="owner/repo",
        local_path=None,
    )
    observed: list[tuple[str, str]] = []

    def fake_plan(*args, **kwargs):
        observed.append(("plan", kwargs["test_network"]))
        return BaselinePlan(repository=repository, steps=())

    def fake_probe(*args, **kwargs):
        observed.append(("check", kwargs["test_network"]))
        return ProbeOutcome(
            receipt={"status": "pass", "plan": {"repository": {}}},
            receipt_path=tmp_path / "receipt.json",
            cached=False,
        )

    monkeypatch.setattr(cli, "create_plan", fake_plan)
    monkeypatch.setattr(cli, "probe_repository", fake_probe)

    assert (
        cli.main(
            [
                "plan",
                "owner/repo",
                "--test-network",
                "enabled",
                "--json",
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "check",
                "owner/repo",
                "--test-network",
                "enabled",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert observed == [("plan", "enabled"), ("check", "enabled")]


def test_component_scope_is_forwarded_by_plan_and_check(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url=None,
        github_repository="owner/repo",
        local_path=None,
    )
    observed: list[tuple[str, str]] = []

    def fake_plan(*args, **kwargs):
        observed.append(("plan", kwargs["component"]))
        return BaselinePlan(repository=repository, steps=())

    def fake_probe(*args, **kwargs):
        observed.append(("check", kwargs["component"]))
        return ProbeOutcome(
            receipt={"status": "pass", "plan": {"repository": {}}},
            receipt_path=tmp_path / "receipt.json",
            cached=False,
        )

    monkeypatch.setattr(cli, "create_plan", fake_plan)
    monkeypatch.setattr(cli, "probe_repository", fake_probe)

    assert (
        cli.main(
            [
                "plan",
                "owner/repo",
                "--component",
                "apps/web",
                "--json",
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "check",
                "owner/repo",
                "--component",
                "apps/web",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert observed == [("plan", "apps/web"), ("check", "apps/web")]


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


def test_doctor_runner_readiness_probes_are_concurrent(
    monkeypatch,
    capsys,
) -> None:
    barrier = threading.Barrier(2)
    monkeypatch.setattr(cli, "_which", lambda command: f"/tools/{command}")
    monkeypatch.setattr(
        cli,
        "available_runners",
        lambda: {"docker": "29.0", "podman": "5.0"},
    )

    def ready(name: str) -> bool:
        barrier.wait(timeout=1)
        return name == "podman"

    monkeypatch.setattr(cli, "runner_ready", ready)

    assert cli.main(["doctor", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["runners"]["docker"]["ready"] is False
    assert payload["runners"]["podman"]["ready"] is True


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


def test_github_status_json_forwards_exact_ref(
    monkeypatch,
    capsys,
) -> None:
    observed: list[tuple[str, str | None]] = []
    payload = {
        "github_status_version": 1,
        "github_api_version": "2026-03-10",
        "repository": {
            "full_name": "owner/repo",
            "default_branch": "main",
            "archived": False,
            "disabled": False,
            "fork": False,
            "visibility": "public",
        },
        "commit_sha": "a" * 40,
        "ref": "a" * 40,
        "state": "success",
        "checks": {
            "state": "success",
            "total_count": 1,
            "returned_count": 1,
            "truncated": False,
            "counts": {"success": 1},
            "runs": [],
        },
        "legacy_status": {
            "state": "success",
            "total_count": 0,
            "returned_count": 0,
            "truncated": False,
            "contexts": [],
        },
        "rate_limit": {"remaining": 57, "limit": 60},
    }

    def status(target: str, ref: str | None):
        observed.append((target, ref))
        return payload

    monkeypatch.setattr(cli, "github_status", status)

    assert (
        cli.main(
            [
                "github-status",
                "owner/repo",
                "--ref",
                "a" * 40,
                "--json",
            ]
        )
        == 0
    )

    assert observed == [("owner/repo", "a" * 40)]
    assert json.loads(capsys.readouterr().out) == payload


def test_github_status_text_surfaces_only_non_successful_checks(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        cli,
        "github_status",
        lambda *args: {
            "repository": {
                "full_name": "owner/repo",
                "archived": True,
                "disabled": False,
                "fork": False,
            },
            "commit_sha": "a" * 40,
            "state": "failure",
            "checks": {
                "state": "failure",
                "total_count": 2,
                "counts": {"failure": 1, "success": 1},
                "runs": [
                    {
                        "name": "test",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "failure",
                    },
                ],
            },
            "legacy_status": {"state": "success", "total_count": 1},
            "rate_limit": {
                "remaining": 57,
                "limit": 60,
                "reset_at": "2026-01-01T00:00:00+00:00",
            },
        },
    )

    assert cli.main(["github-status", "owner/repo"]) == 0

    output = capsys.readouterr().out
    assert "Repository flags: archived" in output
    assert "lint: failure" in output
    assert "test: success" not in output
    assert "57/60 remaining" in output


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
                "--max-volume-gib",
                "2.5",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert observed["max_evidence_bytes"] == int(0.5 * 1024**3)
    assert observed["max_evidence_entries"] == 256
    assert observed["max_volume_bytes"] == int(2.5 * 1024**3)


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
