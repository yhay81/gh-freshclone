from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from gh_freshclone.workspace_archive import create_workspace_archive


def _commit_sha(repository: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository: Path, message: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Freshclone Tests",
            "-c",
            "user.email=freshclone@example.invalid",
            "commit",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


def test_workspace_archive_contains_only_exact_committed_tree(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    script = git_repository / "verify.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "verify.sh"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "update-index", "--chmod=+x", "verify.sh"],
        check=True,
        capture_output=True,
    )
    _commit(git_repository, "add executable")
    (git_repository / ".env").write_text("SECRET=not-archived\n", encoding="utf-8")

    destination = tmp_path / "workspace.tar"
    commit_sha = _commit_sha(git_repository)
    create_workspace_archive(git_repository, destination, commit_sha)

    commit_time = int(
        subprocess.run(
            [
                "git",
                "-C",
                str(git_repository),
                "show",
                "-s",
                "--format=%ct",
                commit_sha,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    with tarfile.open(destination) as archive:
        names = archive.getnames()
        script_info = archive.getmember("verify.sh")

    assert "pyproject.toml" in names
    assert "tests/test_sample.py" in names
    assert ".env" not in names
    assert not any(name == ".git" or name.startswith(".git/") for name in names)
    assert script_info.mode == 0o755
    assert script_info.mtime == commit_time


def test_workspace_archive_reads_exact_commit_not_modified_worktree(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    (git_repository / "pyproject.toml").write_text(
        "[project]\nname='modified'\nversion='1.0.0'\n",
        encoding="utf-8",
    )

    destination = tmp_path / "workspace.tar"
    create_workspace_archive(
        git_repository,
        destination,
        _commit_sha(git_repository),
    )

    with tarfile.open(destination) as archive:
        content = archive.extractfile("pyproject.toml")
        assert content is not None
        value = content.read().decode("utf-8")
    assert 'name = "sample"' in value
    assert "name='modified'" not in value


def test_workspace_archive_can_limit_contents_to_one_component(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    component = git_repository / "apps" / "web"
    component.mkdir(parents=True)
    (component / "package.json").write_text("{}\n", encoding="utf-8")
    (component / "source.js").write_text("export const value = 1;\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
        check=True,
        capture_output=True,
    )
    _commit(git_repository, "add component")
    destination = tmp_path / "component.tar"

    create_workspace_archive(
        git_repository,
        destination,
        _commit_sha(git_repository),
        component="apps/web",
    )

    with tarfile.open(destination) as archive:
        names = archive.getnames()
    assert names == [
        "apps/web/package.json",
        "apps/web/source.js",
    ]


@pytest.mark.skipif(os.name == "nt", reason="Git stores symlinks as files on Windows")
def test_workspace_archive_preserves_committed_symlink(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    (git_repository / "sample-link").symlink_to("tests/test_sample.py")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "sample-link"],
        check=True,
        capture_output=True,
    )
    _commit(git_repository, "add symlink")

    destination = tmp_path / "workspace.tar"
    create_workspace_archive(
        git_repository,
        destination,
        _commit_sha(git_repository),
    )

    with tarfile.open(destination) as archive:
        link = archive.getmember("sample-link")
    assert link.issym()
    assert link.linkname == "tests/test_sample.py"
