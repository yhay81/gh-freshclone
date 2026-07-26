from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .configuration import CONFIG_NAME
from .model import Repository
from .process import CommandError, run

_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_SSH_URL = re.compile(
    r"^(?:ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class RepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubTarget:
    full_name: str
    ref: str | None = None


def parse_github_target(value: str) -> GitHubTarget | None:
    """Parse a repository, commit URL, or pull-request URL without I/O."""

    candidate = value.strip()
    if _OWNER_REPO.fullmatch(candidate):
        return GitHubTarget(candidate.removesuffix(".git"))

    match = _GITHUB_SSH_URL.fullmatch(candidate)
    if match:
        return GitHubTarget(f"{match.group('owner')}/{match.group('repo')}")

    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repository = parts[:2]
    repository = repository.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repository
    ):
        return None
    full_name = f"{owner}/{repository}"
    if len(parts) == 2:
        return GitHubTarget(full_name)
    if len(parts) == 4 and parts[2].lower() == "commit" and _SHA.fullmatch(parts[3]):
        return GitHubTarget(full_name, parts[3].lower())
    if (
        len(parts) in {4, 5}
        and parts[2].lower() == "pull"
        and parts[3].isdigit()
        and (len(parts) == 4 or parts[4].lower() == "files")
    ):
        pull_number = parts[3].lstrip("0")
        if not pull_number or len(pull_number) > 20:
            return None
        return GitHubTarget(full_name, f"refs/pull/{pull_number}/head")
    return None


def parse_github_repository(value: str) -> str | None:
    target = parse_github_target(value)
    return target.full_name if target else None


def _require(command: str) -> None:
    if shutil.which(command) is None:
        raise RepositoryError(f"required command is not installed: {command}")


@contextmanager
def _isolated_git_environment(directory: Path | None = None) -> Iterator[dict[str, str]]:
    """Yield a Git environment that cannot consult host credentials or config."""

    if directory is None:
        handle, name = tempfile.mkstemp(prefix="gh-freshclone-gitconfig-")
        os.close(handle)
        empty_config = Path(name)
    else:
        directory.mkdir(parents=True, exist_ok=True)
        empty_config = directory / f".ghfc-gitconfig-{uuid.uuid4().hex}"
        try:
            empty_config.write_text("", encoding="utf-8")
        except OSError as exc:
            raise RepositoryError("could not create an isolated Git configuration") from exc

    environment = os.environ.copy()
    for name in tuple(environment):
        normalized = name.upper()
        if normalized.startswith("GIT_") or normalized == "SSH_ASKPASS":
            environment.pop(name, None)
    environment.update(
        {
            "GCM_INTERACTIVE": "Never",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": str(empty_config),
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        yield environment
    finally:
        with suppress(OSError):
            empty_config.unlink(missing_ok=True)


def _git_root_and_sha(path: Path, ref: str) -> tuple[Path, str]:
    try:
        output = run(
            [
                "git",
                "-C",
                str(path),
                "rev-parse",
                "--show-toplevel",
                "--verify",
                "--end-of-options",
                f"{ref}^{{commit}}",
            ]
        ).stdout.splitlines()
    except CommandError as exc:
        raise RepositoryError(
            f"path is not a Git repository or ref does not resolve: {path}@{ref}"
        ) from exc
    if len(output) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", output[1]):
        raise RepositoryError(f"Git returned unexpected repository metadata: {path}")
    return Path(output[0]).resolve(), output[1].lower()


def _git_remote(root: Path) -> str | None:
    result = run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _resolve_local(path: Path, ref: str | None) -> Repository:
    _require("git")
    selected_ref = ref or "HEAD"
    root, sha = _git_root_and_sha(path, selected_ref)
    remote = _git_remote(root)
    github_repository = parse_github_repository(remote or "")
    source_url = (
        f"https://github.com/{github_repository}" if github_repository else None
    )
    return Repository(
        display_name=github_repository or root.name,
        commit_sha=sha,
        ref=selected_ref,
        source_url=source_url,
        github_repository=github_repository,
        local_path=str(root),
    )


def _validate_remote_ref(ref: str) -> None:
    if (
        not ref
        or len(ref) > 1024
        or ref.startswith("-")
        or ref.endswith(("/", "."))
        or ".." in ref
        or "@{" in ref
        or "//" in ref
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
        or any(character in ref for character in " ~^:?*[\\")
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in ref.split("/")
        )
    ):
        raise RepositoryError(f"invalid Git ref: {ref!r}")


def _remote_refs(full_name: str, patterns: tuple[str, ...], *, symref: bool) -> str:
    _require("git")
    command = ["git", "ls-remote"]
    if symref:
        command.append("--symref")
    command.extend(
        (
            "--exit-code",
            "--",
            f"https://github.com/{full_name}.git",
            *patterns,
        )
    )
    try:
        with _isolated_git_environment() as environment:
            return run(command, env=environment).stdout
    except CommandError as exc:
        raise RepositoryError(
            f"could not resolve public GitHub repository or ref: {full_name}"
        ) from exc


def _parse_remote_refs(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        try:
            value, name = line.split("\t", 1)
        except ValueError:
            continue
        if _SHA.fullmatch(value):
            refs[name] = value.lower()
    return refs


def _resolve_github(full_name: str, ref: str | None) -> Repository:
    if ref is None:
        output = _remote_refs(full_name, ("HEAD",), symref=True)
        refs = _parse_remote_refs(output)
        branch_match = re.search(r"^ref: refs/heads/(.+)\tHEAD$", output, re.MULTILINE)
        sha = refs.get("HEAD", "")
        if not branch_match or not _SHA.fullmatch(sha):
            raise RepositoryError(
                f"GitHub did not advertise a default-branch commit for {full_name}"
            )
        branch = branch_match.group(1)
        _validate_remote_ref(branch)
        return Repository(
            display_name=full_name,
            commit_sha=sha,
            ref=branch,
            source_url=f"https://github.com/{full_name}",
            github_repository=full_name,
            local_path=None,
            is_private=False,
        )

    selected_ref = ref
    if _SHA.fullmatch(selected_ref):
        return Repository(
            display_name=full_name,
            commit_sha=selected_ref.lower(),
            ref=selected_ref,
            source_url=f"https://github.com/{full_name}",
            github_repository=full_name,
            local_path=None,
            is_private=None,
        )

    _validate_remote_ref(selected_ref)
    if selected_ref.startswith("refs/"):
        patterns = (selected_ref,)
        candidates = (selected_ref,)
    else:
        branch = f"refs/heads/{selected_ref}"
        tag = f"refs/tags/{selected_ref}"
        patterns = (branch, tag, f"{tag}^{{}}")
        candidates = (branch, f"{tag}^{{}}", tag)
    refs = _parse_remote_refs(_remote_refs(full_name, patterns, symref=False))
    resolved = next(
        ((candidate, refs[candidate]) for candidate in candidates if candidate in refs),
        None,
    )
    if not resolved:
        raise RepositoryError(f"GitHub ref does not resolve: {full_name}@{selected_ref}")
    _, sha = resolved
    return Repository(
        display_name=full_name,
        commit_sha=sha,
        ref=selected_ref,
        source_url=f"https://github.com/{full_name}",
        github_repository=full_name,
        local_path=None,
        is_private=False,
    )


def resolve_repository(target: str, ref: str | None = None) -> Repository:
    """Resolve a local path or GitHub repository to an immutable commit."""

    path = Path(target).expanduser()
    if path.exists():
        return _resolve_local(path, ref)
    github_target = parse_github_target(target)
    if github_target:
        if ref is not None and github_target.ref is not None:
            raise RepositoryError(
                "--ref cannot be combined with a GitHub commit or pull-request URL"
            )
        return _resolve_github(github_target.full_name, ref or github_target.ref)
    raise RepositoryError(
        "target is neither an existing path nor a GitHub repository, "
        "commit, or pull-request URL"
    )


def _commit_exists(
    destination: Path,
    commit_sha: str,
    environment: dict[str, str],
) -> bool:
    present = run(
        ["git", "-C", str(destination), "cat-file", "-e", commit_sha],
        check=False,
        env=environment,
    )
    return present.returncode == 0


def _fetch_shallow(
    destination: Path,
    target: str,
    environment: dict[str, str],
) -> None:
    run(
        [
            "git",
            "-C",
            str(destination),
            "fetch",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            "--no-tags",
            "--",
            "origin",
            target,
        ],
        env=environment,
    )


def _materialize_remote(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> None:
    if not repository.github_repository:
        raise RepositoryError("repository has no public GitHub source")
    run(
        [
            "git",
            "init",
            "--quiet",
            "--template=",
            "--",
            str(destination),
        ],
        env=environment,
    )
    run(
        [
            "git",
            "-C",
            str(destination),
            "remote",
            "add",
            "origin",
            f"https://github.com/{repository.github_repository}.git",
        ],
        env=environment,
    )
    fetch_target = repository.ref or repository.commit_sha
    _fetch_shallow(destination, fetch_target, environment)
    commit_exists = _commit_exists(
        destination,
        repository.commit_sha,
        environment,
    )
    if not commit_exists and fetch_target != repository.commit_sha:
        _fetch_shallow(destination, repository.commit_sha, environment)
        commit_exists = _commit_exists(
            destination,
            repository.commit_sha,
            environment,
        )
    if not commit_exists:
        raise RepositoryError(
            f"resolved commit is no longer available: {repository.display_name}"
        )


def _validate_materialization(
    repository: Repository,
    destination: Path,
) -> None:
    if destination.exists():
        raise RepositoryError(f"checkout destination already exists: {destination}")
    if repository.is_private:
        raise RepositoryError(
            "private GitHub repositories are not supported by the "
            "credential-free checkout policy"
        )
    if not repository.local_path and not repository.github_repository:
        raise RepositoryError("repository has no materializable source")


def _initialize_materialization(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> None:
    if repository.local_path:
        run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-hardlinks",
                "--no-checkout",
                "--template=",
                "--",
                repository.local_path,
                str(destination),
            ],
            env=environment,
        )
    else:
        _materialize_remote(repository, destination, environment)
    if repository.local_path and not _commit_exists(
        destination,
        repository.commit_sha,
        environment,
    ):
        raise RepositoryError(
            f"resolved commit is no longer available: {repository.display_name}"
        )


def _checkout_complete(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> None:
    run(
        [
            "git",
            "-C",
            str(destination),
            "checkout",
            "--quiet",
            "--detach",
            "--force",
            repository.commit_sha,
        ],
        env=environment,
    )


def _tree_paths(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> tuple[str, ...]:
    from .detect import (
        AUTOMATIC_PLAN_INPUT_FILES,
        AUTOMATIC_PLAN_INPUT_SUFFIXES,
        NESTED_MANIFEST_NAMES,
        RUBY_RAKE_TASK_PREFIXES,
    )

    output = run(
        [
            "git",
            "-C",
            str(destination),
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            repository.commit_sha,
        ],
        env=environment,
    ).stdout
    selected: list[str] = []
    for value in output.split("\0"):
        if not value:
            continue
        path = PurePosixPath(value)
        ruby_task = path.suffix.casefold() == ".rake" and (
            len(path.parts) == 1
            or any(value.startswith(prefix) for prefix in RUBY_RAKE_TASK_PREFIXES)
        )
        if (
            value in AUTOMATIC_PLAN_INPUT_FILES
            or (
                len(path.parts) == 1
                and path.suffix.casefold() in AUTOMATIC_PLAN_INPUT_SUFFIXES
            )
        ) or (
            len(path.parts) == 3 and path.name in NESTED_MANIFEST_NAMES
        ) or ruby_task:
            selected.append(value)
    return tuple(selected)


def _portable_checkout_path(value: str) -> PurePosixPath | None:
    if (
        not value
        or len(value) > (180 if os.name == "nt" else 4096)
        or "\\" in value
        or "\ufffd" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    for part in path.parts:
        if len(part.encode("utf-8")) > 255:
            return None
        if os.name == "nt" and (
            part.endswith((" ", "."))
            or any(character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            return None
    return path


def _checkout_paths(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
    paths: tuple[str, ...],
) -> None:
    for offset in range(0, len(paths), 64):
        batch = paths[offset : offset + 64]
        run(
            [
                "git",
                "-C",
                str(destination),
                "checkout",
                "--quiet",
                repository.commit_sha,
                "--",
                *(f":(top,literal){path}" for path in batch),
            ],
            env=environment,
        )


def _normalized_symlink_target(
    link: PurePosixPath,
    target: str,
) -> PurePosixPath | None:
    if (
        not target
        or "\\" in target
        or PurePosixPath(target).is_absolute()
        or any(ord(character) < 32 or ord(character) == 127 for character in target)
    ):
        return None
    parts = list(link.parent.parts)
    for part in PurePosixPath(target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return _portable_checkout_path(PurePosixPath(*parts).as_posix())


def _tree_object_type(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
    path: str,
) -> str | None:
    result = run(
        [
            "git",
            "-C",
            str(destination),
            "cat-file",
            "-t",
            f"{repository.commit_sha}:{path}",
        ],
        check=False,
        env=environment,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _checkout_symlink_targets(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
    paths: tuple[str, ...],
) -> None:
    pending = list(paths)
    seen = set(paths)
    while pending:
        value = pending.pop()
        path = PurePosixPath(value)
        materialized = destination.joinpath(*path.parts)
        if not materialized.is_symlink():
            continue
        try:
            target_value = os.readlink(materialized)
        except OSError:
            continue
        target = _normalized_symlink_target(path, target_value)
        if (
            target is None
            or target.as_posix() in seen
            or _tree_object_type(
                repository,
                destination,
                environment,
                target.as_posix(),
            )
            != "blob"
        ):
            continue
        selected = target.as_posix()
        seen.add(selected)
        _checkout_paths(
            repository,
            destination,
            environment,
            (selected,),
        )
        pending.append(selected)


def _node_entrypoint_paths(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> tuple[str, ...]:
    package_path = destination / "package.json"
    if not package_path.is_file():
        return ()
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(package, dict):
        return ()
    selected: list[str] = []
    for field in ("main", "module", "types", "typings"):
        value = package.get(field)
        if not isinstance(value, str):
            continue
        candidate = value.strip().removeprefix("./")
        path = _portable_checkout_path(candidate)
        if (
            path is not None
            and _tree_object_type(
                repository,
                destination,
                environment,
                path.as_posix(),
            )
            == "blob"
        ):
            selected.append(path.as_posix())
    return tuple(dict.fromkeys(selected))


def _dotnet_project_paths(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> tuple[str, ...]:
    from .detect import _dotnet_solution_projects

    solutions = sorted(
        (
            *destination.glob("*.sln"),
            *destination.glob("*.slnx"),
        ),
        key=lambda path: path.name.casefold(),
    )
    if len(solutions) != 1:
        return ()
    projects = _dotnet_solution_projects(solutions[0])
    if len(projects) > 256:
        return ()
    available = set(
        run(
            [
                "git",
                "-C",
                str(destination),
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                repository.commit_sha,
            ],
            env=environment,
        ).stdout.split("\0")
    )
    return tuple(
        project
        for project in projects
        if (
            _portable_checkout_path(project) is not None
            and project in available
        )
    )


def _materialize_automatic_plan_inputs(
    repository: Repository,
    destination: Path,
    environment: dict[str, str],
) -> bool:
    from .detect import (
        AUTOMATIC_PLAN_INPUT_FILES,
        AUTOMATIC_PLAN_INPUT_SUFFIXES,
        NESTED_MANIFEST_NAMES,
        RUBY_RAKE_TASK_PREFIXES,
    )

    tree_paths = _tree_paths(repository, destination, environment)
    if CONFIG_NAME in tree_paths:
        return False

    automatic_inputs = set(AUTOMATIC_PLAN_INPUT_FILES)
    root_files = tuple(
        path
        for path in tree_paths
        if (
            path in automatic_inputs
            or (
                len(PurePosixPath(path).parts) == 1
                and PurePosixPath(path).suffix.casefold()
                in AUTOMATIC_PLAN_INPUT_SUFFIXES
            )
        )
        and _portable_checkout_path(path) is not None
    )
    _checkout_paths(
        repository,
        destination,
        environment,
        root_files,
    )
    _checkout_symlink_targets(
        repository,
        destination,
        environment,
        root_files,
    )
    ruby_tasks: list[str] = []
    if "Gemfile.lock" in tree_paths and "Rakefile" in tree_paths:
        seen_ruby_tasks: set[str] = set()
        for value in tree_paths:
            path = _portable_checkout_path(value)
            if (
                path is None
                or path.suffix.casefold() != ".rake"
                or not (
                    len(path.parts) == 1
                    or any(
                        value.startswith(prefix)
                        for prefix in RUBY_RAKE_TASK_PREFIXES
                    )
                )
            ):
                continue
            collision_key = (
                path.as_posix().casefold() if os.name == "nt" else path.as_posix()
            )
            if collision_key in seen_ruby_tasks:
                continue
            seen_ruby_tasks.add(collision_key)
            ruby_tasks.append(path.as_posix())
            if len(ruby_tasks) == 128:
                break
    _checkout_paths(
        repository,
        destination,
        environment,
        tuple(ruby_tasks),
    )
    _checkout_symlink_targets(
        repository,
        destination,
        environment,
        tuple(ruby_tasks),
    )
    entrypoints = _node_entrypoint_paths(
        repository,
        destination,
        environment,
    )
    _checkout_paths(
        repository,
        destination,
        environment,
        entrypoints,
    )
    _checkout_symlink_targets(
        repository,
        destination,
        environment,
        entrypoints,
    )
    dotnet_projects = _dotnet_project_paths(
        repository,
        destination,
        environment,
    )
    _checkout_paths(
        repository,
        destination,
        environment,
        dotnet_projects,
    )
    _checkout_symlink_targets(
        repository,
        destination,
        environment,
        dotnet_projects,
    )

    for directory in ("tests", "test", "spec"):
        if (
            _tree_object_type(
                repository,
                destination,
                environment,
                directory,
            )
            == "tree"
        ):
            (destination / directory).mkdir(exist_ok=True)

    seen: set[str] = set()
    nested_markers: list[str] = []
    for value in tree_paths:
        path = _portable_checkout_path(value)
        if (
            path is None
            or len(path.parts) != 3
            or path.name not in NESTED_MANIFEST_NAMES
        ):
            continue
        collision_key = path.as_posix().casefold() if os.name == "nt" else path.as_posix()
        if collision_key in seen:
            continue
        seen.add(collision_key)
        nested_markers.append(path.as_posix())
    _checkout_paths(
        repository,
        destination,
        environment,
        tuple(nested_markers),
    )
    return True


def materialize(repository: Repository, destination: Path) -> None:
    """Create a credential-free checkout pinned to ``repository.commit_sha``."""

    _require("git")
    _validate_materialization(repository, destination)
    try:
        with _isolated_git_environment(destination.parent) as environment:
            _initialize_materialization(repository, destination, environment)
            _checkout_complete(repository, destination, environment)
    except CommandError as exc:
        raise RepositoryError(f"failed to check out {repository.display_name}") from exc


def materialize_plan_inputs(repository: Repository, destination: Path) -> None:
    """Materialize only committed files needed for deterministic plan detection."""

    _require("git")
    _validate_materialization(repository, destination)
    try:
        with _isolated_git_environment(destination.parent) as environment:
            _initialize_materialization(repository, destination, environment)
            if not _materialize_automatic_plan_inputs(
                repository,
                destination,
                environment,
            ):
                _checkout_complete(repository, destination, environment)
    except CommandError as exc:
        raise RepositoryError(
            f"failed to inspect committed plan inputs for {repository.display_name}"
        ) from exc


def complete_materialization(
    repository: Repository,
    destination: Path,
) -> None:
    """Expand a plan-only checkout to the complete immutable worktree."""

    _require("git")
    if not destination.is_dir():
        raise RepositoryError(f"checkout destination does not exist: {destination}")
    try:
        with _isolated_git_environment(destination.parent) as environment:
            if not _commit_exists(
                destination,
                repository.commit_sha,
                environment,
            ):
                raise RepositoryError(
                    f"resolved commit is no longer available: {repository.display_name}"
                )
            _checkout_complete(repository, destination, environment)
    except CommandError as exc:
        raise RepositoryError(f"failed to check out {repository.display_name}") from exc
