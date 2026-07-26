from __future__ import annotations

import subprocess  # nosec B404
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import BinaryIO, Self, cast

from .github import _isolated_git_environment
from .model import normalize_component
from .process import CommandError, run


class WorkspaceArchiveError(RuntimeError):
    """The committed checkout could not be represented as a safe POSIX archive."""


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    object_type: str
    object_id: str
    path: PurePosixPath


class _LimitedReader:
    def __init__(self, source: BinaryIO, size: int) -> None:
        self._source = source
        self.remaining = size

    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > self.remaining:
            size = self.remaining
        if size == 0:
            return b""
        value = self._source.read(size)
        self.remaining -= len(value)
        return value


class _GitObjectBatch:
    def __init__(
        self,
        workspace: Path,
        environment: dict[str, str],
    ) -> None:
        batch_environment = environment.copy()
        batch_environment["GIT_NO_LAZY_FETCH"] = "1"
        self._process = subprocess.Popen(  # nosec B603 B607
            [
                "git",
                "-C",
                str(workspace),
                "cat-file",
                "--batch",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=batch_environment,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        stdin = self._process.stdin
        stdout = self._process.stdout
        stderr = self._process.stderr
        if stdin is not None:
            stdin.close()
        if exception_type is not None:
            self._process.terminate()
        returncode = self._process.wait()
        detail = stderr.read().decode("utf-8", errors="replace").strip() if stderr else ""
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        if exception_type is None and returncode != 0:
            raise WorkspaceArchiveError(
                detail or f"git cat-file exited with status {returncode}"
            )

    def _header(self, object_id: str) -> tuple[BinaryIO, int]:
        stdin = self._process.stdin
        stdout = self._process.stdout
        if stdin is None or stdout is None:
            raise WorkspaceArchiveError("Git object stream is unavailable")
        stdin.write(f"{object_id}\n".encode("ascii"))
        stdin.flush()
        header = stdout.readline()
        try:
            returned_id, object_type, size_text = header.rstrip(b"\n").split(b" ", 2)
            size = int(size_text)
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceArchiveError("Git returned malformed object metadata") from exc
        if (
            returned_id.decode("ascii") != object_id
            or object_type != b"blob"
            or size < 0
        ):
            raise WorkspaceArchiveError("Git returned an unexpected object")
        return cast(BinaryIO, stdout), size

    def add_regular(
        self,
        archive: tarfile.TarFile,
        info: tarfile.TarInfo,
        object_id: str,
    ) -> None:
        stdout, size = self._header(object_id)
        info.size = size
        content = _LimitedReader(stdout, size)
        archive.addfile(info, cast(BinaryIO, content))
        if content.remaining != 0 or stdout.read(1) != b"\n":
            raise WorkspaceArchiveError("Git returned a truncated object")

    def read_blob(self, object_id: str, *, maximum_size: int) -> bytes:
        stdout, size = self._header(object_id)
        if size > maximum_size:
            raise WorkspaceArchiveError("committed symbolic-link target is too large")
        content = stdout.read(size)
        if len(content) != size or stdout.read(1) != b"\n":
            raise WorkspaceArchiveError("Git returned a truncated object")
        return content


def _archive_path(value: str) -> PurePosixPath:
    if (
        not value
        or "\\" in value
        or "\ufffd" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WorkspaceArchiveError("checkout contains a non-portable Git path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise WorkspaceArchiveError("checkout contains an unsafe Git path")
    return path


def _tree_entries(
    workspace: Path,
    commit_sha: str,
    environment: dict[str, str],
    component: str,
) -> tuple[_TreeEntry, ...]:
    command = [
        "git",
        "-C",
        str(workspace),
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit_sha,
    ]
    if component != ".":
        command.extend(("--", f":(top,literal){component}"))
    output = run(command, env=environment).stdout
    entries: list[_TreeEntry] = []
    for record in output.split("\0"):
        if not record:
            continue
        try:
            metadata, value = record.split("\t", 1)
            mode, object_type, object_id = metadata.split(" ", 2)
        except ValueError as exc:
            raise WorkspaceArchiveError("Git returned malformed tree metadata") from exc
        if object_type not in {"blob", "commit"}:
            raise WorkspaceArchiveError(
                f"unsupported Git tree object type: {object_type}"
            )
        entries.append(
            _TreeEntry(
                mode=mode,
                object_type=object_type,
                object_id=object_id,
                path=_archive_path(value),
            )
        )
    return tuple(entries)


def _symlink_target(objects: _GitObjectBatch, object_id: str) -> str:
    try:
        content = objects.read_blob(object_id, maximum_size=4096)
        target = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceArchiveError("could not read a committed symbolic link") from exc
    if "\x00" in target:
        raise WorkspaceArchiveError("symbolic-link target contains a NUL byte")
    return target


def _add_entry(
    archive: tarfile.TarFile,
    entry: _TreeEntry,
    objects: _GitObjectBatch,
    commit_time: int,
) -> None:
    name = entry.path.as_posix()
    info = tarfile.TarInfo(name)
    info.mtime = commit_time
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if entry.mode == "160000" and entry.object_type == "commit":
        info.name = f"{name}/"
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        archive.addfile(info)
        return

    if entry.mode == "120000" and entry.object_type == "blob":
        info.type = tarfile.SYMTYPE
        info.mode = 0o777
        info.linkname = _symlink_target(objects, entry.object_id)
        archive.addfile(info)
        return
    if (
        entry.mode not in {"100644", "100755"}
        or entry.object_type != "blob"
    ):
        raise WorkspaceArchiveError(f"unsupported committed file mode at {name}")

    info.type = tarfile.REGTYPE
    info.mode = 0o755 if entry.mode == "100755" else 0o644
    objects.add_regular(archive, info, entry.object_id)


def create_workspace_archive(
    workspace: Path,
    destination: Path,
    commit_sha: str,
    *,
    component: str = ".",
) -> None:
    """Write an atomic tar containing one committed repository scope."""

    workspace = workspace.resolve(strict=True)
    component = normalize_component(component)
    if not workspace.is_dir() or not (workspace / ".git").is_dir():
        raise WorkspaceArchiveError("workspace is not a materialized Git checkout")
    destination = destination.resolve()
    if destination.exists():
        raise WorkspaceArchiveError(f"archive destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with _isolated_git_environment(destination.parent) as environment:
            entries = _tree_entries(
                workspace,
                commit_sha,
                environment,
                component,
            )
            timestamp = run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "show",
                    "-s",
                    "--format=%ct",
                    commit_sha,
                ],
                env=environment,
            ).stdout.strip()
            try:
                commit_time = int(timestamp)
            except ValueError as exc:
                raise WorkspaceArchiveError(
                    "Git returned an invalid commit timestamp"
                ) from exc
            with (
                _GitObjectBatch(workspace, environment) as objects,
                tarfile.open(
                    temporary,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive,
            ):
                for entry in entries:
                    _add_entry(
                        archive,
                        entry,
                        objects,
                        commit_time,
                    )
        temporary.replace(destination)
    except (CommandError, OSError, tarfile.TarError) as exc:
        raise WorkspaceArchiveError("could not create the workspace archive") from exc
    finally:
        temporary.unlink(missing_ok=True)
