from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gh_freshclone import github
from gh_freshclone.detect import detect_plan
from gh_freshclone.github import (
    complete_materialization,
    materialize,
    materialize_plan_inputs,
    parse_github_repository,
    parse_github_target,
    resolve_repository,
)
from gh_freshclone.model import Repository
from gh_freshclone.process import CommandError, Completed


def test_parse_github_repository() -> None:
    assert parse_github_repository("owner/repo") == "owner/repo"
    assert (
        parse_github_repository("https://github.com/owner/repo.git")
        == "owner/repo"
    )
    assert parse_github_repository("git@github.com:owner/repo.git") == "owner/repo"
    assert (
        parse_github_repository("https://github.com/owner/repo/pull/42/files")
        == "owner/repo"
    )
    assert parse_github_repository("C:\\work\\repo") is None


def test_parse_github_commit_and_pull_request_targets() -> None:
    sha = "A" * 40

    commit = parse_github_target(f"https://github.com/Owner/Repo/commit/{sha}")
    pull = parse_github_target("https://github.com/Owner/Repo/pull/0042/files")

    assert commit == github.GitHubTarget("Owner/Repo", sha.lower())
    assert pull == github.GitHubTarget("Owner/Repo", "refs/pull/42/head")
    assert parse_github_target("https://user@github.com/owner/repo") is None
    assert parse_github_target("https://github.com/owner/repo/issues/42") is None
    assert parse_github_target("https://github.com/owner/repo/pull/0") is None
    assert (
        parse_github_target(
            "https://github.com/owner/repo/pull/" + "1" * 21
        )
        is None
    )


def test_resolve_and_materialize_local_commit(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    expected = subprocess.run(
        ["git", "-C", str(git_repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize(repository, checkout)

    assert repository.commit_sha == expected
    assert (checkout / "pyproject.toml").is_file()
    actual = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert actual == expected


def test_plan_materialization_expands_only_after_detection(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "pyproject.toml").is_file()
    assert (checkout / "tests").is_dir()
    assert not (checkout / "tests" / "test_sample.py").exists()

    complete_materialization(repository, checkout)

    assert (checkout / "tests" / "test_sample.py").is_file()
    actual = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert actual == repository.commit_sha


def test_plan_materialization_preserves_configured_layouts(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    configured_source = git_repository / "custom" / "source.txt"
    configured_source.parent.mkdir()
    configured_source.write_text("committed source\n", encoding="utf-8")
    (git_repository / ".gh-freshclone.toml").write_text(
        "version = 1\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
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
            "configured layout",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / ".gh-freshclone.toml").is_file()
    assert (checkout / "custom" / "source.txt").read_text(encoding="utf-8") == (
        "committed source\n"
    )


def test_plan_materialization_keeps_nested_manifest_evidence(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    nested = git_repository / "examples" / "demo" / "package.json"
    nested.parent.mkdir(parents=True)
    nested.write_text('{"private": true}\n', encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
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
            "nested manifest",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "examples" / "demo" / "package.json").read_text(
        encoding="utf-8"
    ) == '{"private": true}\n'
    complete_materialization(repository, checkout)
    assert (checkout / "tests" / "test_sample.py").is_file()


def test_plan_materialization_keeps_dotnet_solution_and_project_manifests(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    (git_repository / "Product.sln").write_text(
        'Project("{FAKE}") = "Tests", "test\\Product.Tests.csproj", "{A}"\n',
        encoding="utf-8",
    )
    project = git_repository / "test" / "Product.Tests.csproj"
    project.parent.mkdir(exist_ok=True)
    project.write_text("<Project />\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
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
            "dotnet solution",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "Product.sln").is_file()
    assert (checkout / "test" / "Product.Tests.csproj").is_file()
    plan = detect_plan(repository, checkout)
    assert any(step.ecosystem == "dotnet" for step in plan.steps)


def test_plan_materialization_keeps_composer_lock_and_phpunit_configuration(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    (git_repository / "composer.json").write_text(
        '{"require-dev":{"phpunit/phpunit":"^11.5"}}\n',
        encoding="utf-8",
    )
    (git_repository / "composer.lock").write_text(
        '{"content-hash":"locked","packages":[],"packages-dev":['
        '{"name":"phpunit/phpunit","version":"11.5.42","type":"library"}]}\n',
        encoding="utf-8",
    )
    (git_repository / "phpunit.xml.dist").write_text(
        "<phpunit />\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
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
            "composer baseline",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "composer.json").is_file()
    assert (checkout / "composer.lock").is_file()
    assert (checkout / "phpunit.xml.dist").is_file()
    plan = detect_plan(repository, checkout)
    assert any(step.ecosystem == "php" for step in plan.steps)


def test_plan_materialization_keeps_bounded_ruby_rake_evidence(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "ruby-bundler"
    shutil.copy2(fixture / "Gemfile", git_repository / "Gemfile")
    shutil.copy2(fixture / "Gemfile.lock", git_repository / "Gemfile.lock")
    (git_repository / "Rakefile").write_text(
        'Dir["tasks/**/*.rake"].sort.each { |task| load task }\n',
        encoding="utf-8",
    )
    task = git_repository / "tasks" / "test.rake"
    task.parent.mkdir()
    task.write_text(
        'require "rake/testtask"\nRake::TestTask.new(:test)\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
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
            "ruby baseline",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "Gemfile").is_file()
    assert (checkout / "Gemfile.lock").is_file()
    assert (checkout / "Rakefile").is_file()
    assert (checkout / "tasks" / "test.rake").is_file()
    plan = detect_plan(repository, checkout)
    ruby = next(step for step in plan.steps if step.ecosystem == "ruby")
    assert "tasks/test.rake" in ruby.evidence


def test_plan_materialization_keeps_declared_node_entrypoints(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    (git_repository / "package.json").write_text(
        '{"main":"dist/index.js","scripts":{"test":"node test.js",'
        '"build":"node build.js"}}\n',
        encoding="utf-8",
    )
    entrypoint = git_repository / "dist" / "index.js"
    entrypoint.parent.mkdir()
    entrypoint.write_text("export default 1;\n", encoding="utf-8")
    (git_repository / "unrelated.js").write_text("secret = 1;\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "."],
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
            "node entrypoint",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "dist" / "index.js").is_file()
    assert not (checkout / "unrelated.js").exists()


@pytest.mark.skipif(os.name == "nt", reason="Git stores symlinks as files on Windows")
def test_plan_materialization_follows_committed_input_symlinks(
    git_repository: Path,
    tmp_path: Path,
) -> None:
    target = git_repository / "config" / "project.toml"
    target.parent.mkdir()
    (git_repository / "pyproject.toml").replace(target)
    (git_repository / "pyproject.toml").symlink_to("config/project.toml")
    subprocess.run(
        ["git", "-C", str(git_repository), "add", "-A"],
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
            "symlinked manifest",
        ],
        check=True,
        capture_output=True,
    )
    repository = resolve_repository(str(git_repository))
    checkout = tmp_path / "checkout"

    materialize_plan_inputs(repository, checkout)

    assert (checkout / "pyproject.toml").is_symlink()
    assert (checkout / "config" / "project.toml").is_file()


def test_materialization_uses_sanitized_git_configuration(
    monkeypatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    repository = resolve_repository(str(git_repository))
    original_run = github.run
    git_environments: list[dict[str, str]] = []
    commands: list[tuple[str, ...]] = []

    def inspecting_run(command, **kwargs):
        commands.append(tuple(command))
        git_environments.append(dict(kwargs["env"]))
        return original_run(command, **kwargs)

    monkeypatch.setattr(github, "run", inspecting_run)

    materialize(repository, tmp_path / "checkout")

    assert "--template=" in commands[0]
    assert git_environments
    for environment in git_environments:
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_ATTR_NOSYSTEM"] == "1"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_ASKPASS" not in environment
        assert not Path(environment["GIT_CONFIG_GLOBAL"]).exists()


def test_local_resolution_ignores_uncommitted_files(git_repository: Path) -> None:
    (git_repository / ".env").write_text("SECRET=do-not-copy\n", encoding="utf-8")

    repository = resolve_repository(str(git_repository))

    assert repository.commit_sha
    assert repository.ref == "HEAD"


def test_local_resolution_never_serializes_remote_credentials(
    git_repository: Path,
) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repository),
            "remote",
            "add",
            "origin",
            "https://user:do-not-record@github.com/owner/private.git",
        ],
        check=True,
        capture_output=True,
    )

    repository = resolve_repository(str(git_repository))
    serialized = str(repository.to_dict())

    assert repository.source_url is None
    assert repository.github_repository is None
    assert "do-not-record" not in serialized


def test_local_resolution_canonicalizes_safe_github_remote(
    git_repository: Path,
) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repository),
            "remote",
            "add",
            "origin",
            "git@github.com:Owner/Repo.git",
        ],
        check=True,
        capture_output=True,
    )

    repository = resolve_repository(str(git_repository))

    assert repository.display_name == "Owner/Repo"
    assert repository.source_url == "https://github.com/Owner/Repo"


def test_invalid_target_error_does_not_echo_possible_secret() -> None:
    target = "https://user:do-not-echo@github.com/owner/repo"

    with pytest.raises(github.RepositoryError) as raised:
        resolve_repository(target)

    assert "do-not-echo" not in str(raised.value)


def test_default_github_resolution_uses_one_credential_free_git_request(
    monkeypatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS",
        "'http.extraHeader=Authorization: bearer do-not-forward'",
    )
    monkeypatch.setenv("GIT_EXEC_PATH", "/untrusted/git-core")
    monkeypatch.setenv("Git_AskPass", "/untrusted/askpass")

    def fake_run(command, **kwargs):
        calls.append((tuple(command), dict(kwargs["env"])))
        return Completed(
            tuple(command),
            0,
            f"ref: refs/heads/main\tHEAD\n{'a' * 40}\tHEAD\n",
            "",
        )

    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(github, "_require", lambda command: None)

    repository = github._resolve_github("owner/repo", None)

    assert len(calls) == 1
    command, environment = calls[0]
    assert command[:2] == ("git", "ls-remote")
    assert "--symref" in command
    assert command[-2:] == ("https://github.com/owner/repo.git", "HEAD")
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_ASKPASS" not in environment
    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert "GIT_EXEC_PATH" not in environment
    assert not any(name.upper() == "GIT_ASKPASS" for name in environment)
    assert not Path(environment["GIT_CONFIG_GLOBAL"]).exists()
    assert repository.display_name == "owner/repo"
    assert repository.ref == "main"
    assert repository.commit_sha == "a" * 40
    assert repository.is_private is False


def test_exact_sha_resolution_skips_remote_request(monkeypatch) -> None:
    monkeypatch.setattr(
        github,
        "run",
        lambda *args, **kwargs: pytest.fail("exact SHA must not resolve remotely"),
    )
    sha = "b" * 40

    repository = github._resolve_github("owner/repo", sha)

    assert repository.commit_sha == sha
    assert repository.is_private is None


def test_branch_and_annotated_tag_resolution_are_deterministic(monkeypatch) -> None:
    outputs = iter(
        (
            Completed(
                ("git",),
                0,
                f"{'a' * 40}\trefs/heads/release\n"
                f"{'b' * 40}\trefs/tags/release\n"
                f"{'c' * 40}\trefs/tags/release^{{}}\n",
                "",
            ),
            Completed(
                ("git",),
                0,
                f"{'b' * 40}\trefs/tags/v1\n"
                f"{'c' * 40}\trefs/tags/v1^{{}}\n",
                "",
            ),
        )
    )
    monkeypatch.setattr(github, "run", lambda *args, **kwargs: next(outputs))
    monkeypatch.setattr(github, "_require", lambda command: None)

    branch = github._resolve_github("owner/repo", "release")
    tag = github._resolve_github("owner/repo", "v1")

    assert branch.commit_sha == "a" * 40
    assert tag.commit_sha == "c" * 40


def test_pull_request_url_resolves_advertised_head(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        assert command[-1] == "refs/pull/42/head"
        return Completed(tuple(command), 0, f"{'d' * 40}\trefs/pull/42/head\n", "")

    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(github, "_require", lambda command: None)

    repository = resolve_repository("https://github.com/owner/repo/pull/42")

    assert repository.ref == "refs/pull/42/head"
    assert repository.commit_sha == "d" * 40


def test_embedded_ref_rejects_explicit_ref() -> None:
    with pytest.raises(github.RepositoryError, match="cannot be combined"):
        resolve_repository(
            f"https://github.com/owner/repo/commit/{'a' * 40}",
            "main",
        )


def test_unavailable_remote_is_reported_as_public_resolution_failure(
    monkeypatch,
) -> None:
    def fail(command, **kwargs):
        raise CommandError(command, 128, "repository not found")

    monkeypatch.setattr(github, "run", fail)
    monkeypatch.setattr(github, "_require", lambda command: None)

    with pytest.raises(github.RepositoryError, match="could not resolve public"):
        github._resolve_github("owner/private", None)


def test_public_github_materialization_fetches_only_the_resolved_ref(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
        is_private=False,
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        commands.append(tuple(command))
        return Completed(tuple(command), 0, "", "")

    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(github, "_require", lambda command: None)

    materialize(repository, tmp_path / "checkout")

    assert commands[0][0:2] == ("git", "init")
    assert commands[1][-2:] == (
        "origin",
        "https://github.com/owner/repo.git",
    )
    fetch = commands[2]
    assert fetch[0:3] == ("git", "-C", str(tmp_path / "checkout"))
    assert "fetch" in fetch
    assert "--depth=1" in fetch
    assert "--filter=blob:none" in fetch
    assert "--no-tags" in fetch
    assert fetch[-2:] == ("origin", "main")
    assert not any("clone" in command for command in commands)
    assert all(command[0] == "git" for command in commands)


def test_public_materialization_ref_move_falls_back_to_exact_commit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
        is_private=False,
    )
    commands: list[tuple[str, ...]] = []
    exact_fetched = False

    def fake_run(command, **kwargs):
        nonlocal exact_fetched
        normalized = tuple(command)
        commands.append(normalized)
        if "fetch" in normalized and normalized[-1] == repository.commit_sha:
            exact_fetched = True
        if "cat-file" in normalized and not exact_fetched:
            return Completed(normalized, 1, "", "")
        return Completed(normalized, 0, "", "")

    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(github, "_require", lambda command: None)

    materialize(repository, tmp_path / "checkout")

    fetches = [command for command in commands if "fetch" in command]
    assert [command[-1] for command in fetches] == [
        "main",
        repository.commit_sha,
    ]
    assert all("--depth=1" in command for command in fetches)
    assert commands[-1][-3:] == ("--detach", "--force", repository.commit_sha)


def test_public_materialization_exact_sha_fetches_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commit_sha = "a" * 40
    repository = Repository(
        display_name="owner/repo",
        commit_sha=commit_sha,
        ref=commit_sha,
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
        is_private=None,
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        normalized = tuple(command)
        commands.append(normalized)
        return Completed(normalized, 0, "", "")

    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(github, "_require", lambda command: None)

    materialize(repository, tmp_path / "checkout")

    fetches = [command for command in commands if "fetch" in command]
    assert len(fetches) == 1
    assert fetches[0][-2:] == ("origin", commit_sha)


def test_public_materialization_fails_when_resolved_commit_disappears(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
        is_private=False,
    )

    def fake_run(command, **kwargs):
        normalized = tuple(command)
        if "cat-file" in normalized:
            return Completed(normalized, 1, "", "")
        return Completed(normalized, 0, "", "")

    monkeypatch.setattr(github, "run", fake_run)
    monkeypatch.setattr(github, "_require", lambda command: None)

    with pytest.raises(github.RepositoryError, match="no longer available"):
        materialize(repository, tmp_path / "checkout")


def test_private_github_materialization_fails_before_git(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = Repository(
        display_name="owner/private",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/private",
        github_repository="owner/private",
        local_path=None,
        is_private=True,
    )
    monkeypatch.setattr(github, "_require", lambda command: None)

    with pytest.raises(github.RepositoryError, match="private GitHub repositories"):
        materialize(repository, tmp_path / "checkout")
