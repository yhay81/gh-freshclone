from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .constants import (
    AUTOMATIC_PRUNE_INTERVAL_SECONDS,
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_EVIDENCE_BYTES,
    DEFAULT_MAX_EVIDENCE_ENTRIES,
    DEFAULT_MAX_VOLUMES,
)
from .model import EXECUTION_POLICY_VERSION, RECEIPT_VERSION
from .process import run
from .receipts import cache_namespace, cache_root

_VOLUME_NAME = re.compile(
    r"ghfc-[a-f0-9]{8,12}-[a-f0-9]{12}-[A-Za-z0-9_.-]+$"
)
_LAST_USED = ".gh-freshclone-last-used"
_CACHE_STATE_LOCK_KEY = "gh-freshclone-cache-state-v1"


@dataclass(frozen=True)
class CacheReport:
    host_bytes: int
    host_entries: int
    prepared_volumes: int
    evidence_bytes: int = 0
    evidence_entries: int = 0
    removed_bytes: int = 0
    removed_entries: int = 0
    removed_evidence_bytes: int = 0
    removed_evidence_entries: int = 0
    removed_volumes: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _HostEntry:
    path: Path
    size: int
    last_used: float


@dataclass(frozen=True)
class _EvidenceEntry:
    files: tuple[Path, ...]
    size: int
    last_used: float
    stale: bool = False


def _registry_path() -> Path:
    return cache_root() / "cache-registry.json"


@contextmanager
def cache_lock(key: str) -> Iterator[None]:
    """Serialize matching probes across processes without a daemon."""

    digest = hashlib.sha256(key.encode()).hexdigest()
    path = cache_root() / "locks" / f"{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _normalized_lock_path(path: Path) -> str:
    value = str(path.absolute())
    return value.casefold() if os.name == "nt" else value


@contextmanager
def cache_path_lock(path: Path) -> Iterator[None]:
    key = f"gh-freshclone-path-resource-v1\0{_normalized_lock_path(path)}"
    with cache_lock(key):
        yield


@contextmanager
def cache_volume_lock(runner: str, name: str) -> Iterator[None]:
    key = f"gh-freshclone-volume-resource-v1\0{runner}\0{name}"
    with cache_lock(key):
        yield


def _read_registry() -> list[dict[str, Any]]:
    try:
        value = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    volumes = value.get("volumes") if isinstance(value, dict) else None
    return [item for item in volumes if isinstance(item, dict)] if isinstance(volumes, list) else []


def _write_registry(volumes: list[dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"version": 1, "volumes": volumes}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _discover_managed_volumes() -> list[dict[str, Any]]:
    """Recover Docker/Podman volume ownership from labels if the ledger is lost."""

    discovered: list[dict[str, Any]] = []
    for runner in ("docker", "podman"):
        if shutil.which(runner) is None:
            continue
        listed = run(
            [
                runner,
                "volume",
                "ls",
                "--filter",
                "label=gh-freshclone.managed=true",
                "--filter",
                f"label=gh-freshclone.cache-id={cache_namespace()}",
                "--format",
                "{{.Name}}",
            ],
            check=False,
        )
        if listed.returncode != 0:
            continue
        discovered_at = time.time()
        for name in listed.stdout.splitlines():
            name = name.strip()
            if _VOLUME_NAME.fullmatch(name):
                discovered.append(
                    {
                        "runner": runner,
                        "name": name,
                        "created_at": discovered_at,
                        "last_used_at": discovered_at,
                    }
                )
    return discovered


def _volume_state(runner: str, name: str) -> bool | None:
    """Return True/False for known state, or None when the runner is unavailable."""

    if runner not in {"docker", "podman", "container"} or not _VOLUME_NAME.fullmatch(name):
        return False
    if shutil.which(runner) is None:
        return None
    inspected = run([runner, "volume", "inspect", name], check=False)
    if inspected.returncode == 0:
        return True
    detail = (inspected.stderr or inspected.stdout).lower()
    if any(
        marker in detail
        for marker in (
            "no such volume",
            "volume not found",
            "does not exist",
        )
    ):
        return False
    return None


def _managed_volumes() -> list[dict[str, Any]]:
    volumes = [
        item
        for item in _read_registry()
        if (
            _volume_state(
                str(item.get("runner")),
                str(item.get("name")),
            )
            is not False
        )
    ]
    keys = {
        (str(item.get("runner")), str(item.get("name")))
        for item in volumes
    }
    for item in _discover_managed_volumes():
        key = (str(item["runner"]), str(item["name"]))
        if key not in keys:
            volumes.append(item)
            keys.add(key)
    return volumes


def touch_dependency_cache(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / _LAST_USED).touch()


def record_prepared_volume(runner: str, name: str) -> None:
    if runner not in {"docker", "podman", "container"} or not _VOLUME_NAME.fullmatch(name):
        return
    with cache_lock(_CACHE_STATE_LOCK_KEY):
        now = time.time()
        volumes = _read_registry()
        for item in volumes:
            if item.get("runner") == runner and item.get("name") == name:
                item["last_used_at"] = now
                _write_registry(volumes)
                return
        volumes.append(
            {
                "runner": runner,
                "name": name,
                "created_at": now,
                "last_used_at": now,
            }
        )
        _write_registry(volumes)


def _forget_prepared_volume(runner: str, name: str) -> None:
    with cache_lock(_CACHE_STATE_LOCK_KEY):
        volumes = _read_registry()
        retained = [
            item
            for item in volumes
            if not (item.get("runner") == runner and item.get("name") == name)
        ]
        if retained != volumes:
            _write_registry(retained)


def _path_size(path: Path) -> int:
    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _host_entries() -> list[_HostEntry]:
    root = cache_root() / "runner-cache"
    entries: list[_HostEntry] = []
    for path in root.glob("*/*/*/*"):
        if not path.is_dir():
            continue
        marker = path / _LAST_USED
        try:
            last_used = marker.stat().st_mtime if marker.exists() else path.stat().st_mtime
        except OSError:
            continue
        entries.append(_HostEntry(path, _path_size(path), last_used))
    return entries


def _evidence_entries() -> list[_EvidenceEntry]:
    """Group each receipt with its bounded logs and retain interrupted-run logs."""

    root = cache_root() / "receipts"
    entries: list[_EvidenceEntry] = []
    claimed_logs: set[Path] = set()
    for receipt in root.glob("*/*.json"):
        if not receipt.is_file() or receipt.is_symlink():
            continue
        logs = tuple(
            path
            for path in receipt.parent.glob(f"{receipt.stem}-*.log")
            if path.is_file() and not path.is_symlink()
        )
        files = (receipt, *logs)
        try:
            sizes = [path.stat().st_size for path in files]
            last_used = max(path.stat().st_mtime for path in files)
        except OSError:
            continue
        try:
            value = json.loads(receipt.read_text(encoding="utf-8"))
            stale = not isinstance(value, dict) or (
                value.get("receipt_version") != RECEIPT_VERSION
                or value.get("execution_policy_version")
                != EXECUTION_POLICY_VERSION
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            stale = True
        claimed_logs.update(logs)
        entries.append(_EvidenceEntry(files, sum(sizes), last_used, stale))

    for log in root.glob("*/*.log"):
        if log in claimed_logs or not log.is_file() or log.is_symlink():
            continue
        try:
            metadata = log.stat()
        except OSError:
            continue
        entries.append(_EvidenceEntry((log,), metadata.st_size, metadata.st_mtime))
    return entries


def _index_files() -> list[Path]:
    return [
        path
        for path in (cache_root() / "indexes").glob("*/*.json")
        if path.is_file() and not path.is_symlink()
    ]


def _index_bytes() -> int:
    total = 0
    for path in _index_files():
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _index_bytes_by_receipt() -> dict[Path, int]:
    cache = cache_root().resolve()
    result: dict[Path, int] = {}
    for index in _index_files():
        try:
            value = json.loads(index.read_text(encoding="utf-8"))
            relative = value.get("receipt") if isinstance(value, dict) else None
            if not isinstance(relative, str):
                continue
            receipt = (cache / relative).resolve()
            if not receipt.is_relative_to(cache):
                continue
            result[receipt] = result.get(receipt, 0) + index.stat().st_size
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return result


def _safe_unlink(path: Path, root: Path) -> tuple[bool, str | None]:
    candidate = path.absolute()
    root = root.absolute()
    if not candidate.is_relative_to(root) or len(candidate.parts) <= len(root.parts):
        return False, "path is outside the app evidence root"
    try:
        if path.is_symlink():
            path.unlink()
        else:
            resolved_root = root.resolve()
            if not path.resolve().is_relative_to(resolved_root):
                return False, "resolved path is outside the app evidence root"
            path.unlink()
    except OSError as exc:
        return False, str(exc)
    return True, None


def _remove_evidence_entry(entry: _EvidenceEntry) -> tuple[bool, str | None]:
    root = cache_root() / "receipts"
    removed_any = False
    errors: list[str] = []
    for path in entry.files:
        removed, error = _safe_unlink(path, root)
        removed_any = removed_any or removed
        if error:
            errors.append(f"{path.name}: {error}")
    return removed_any, "; ".join(errors) or None


def _prune_dangling_indexes() -> tuple[int, int, tuple[str, ...]]:
    """Remove only malformed, escaping, or orphaned app-owned PASS indexes."""

    cache = cache_root().resolve()
    index_root = cache_root() / "indexes"
    removed_bytes = 0
    removed_entries = 0
    warnings: list[str] = []
    for index in _index_files():
        remove = False
        try:
            size = index.stat().st_size
            value = json.loads(index.read_text(encoding="utf-8"))
            relative = value.get("receipt") if isinstance(value, dict) else None
            if not isinstance(relative, str):
                remove = True
            else:
                receipt = (cache / relative).resolve()
                remove = not receipt.is_relative_to(cache) or not receipt.is_file()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            size = 0
            remove = True
        if not remove:
            continue
        removed, error = _safe_unlink(index, index_root)
        if removed:
            removed_bytes += size
            removed_entries += 1
        elif error:
            warnings.append(f"could not remove PASS index {index.name}: {error}")
    return removed_bytes, removed_entries, tuple(warnings)


def _remove_host_entry(path: Path) -> tuple[bool, str | None]:
    root = (cache_root() / "runner-cache").absolute()
    candidate = path.absolute()
    if not candidate.is_relative_to(root) or len(candidate.parts) <= len(root.parts):
        return False, "path is outside the app runner-cache root"
    try:
        if path.is_symlink():
            path.unlink()
        else:
            resolved_root = root.resolve()
            if not path.resolve().is_relative_to(resolved_root):
                return False, "resolved path is outside the app runner-cache root"
            if os.name == "nt":
                _unlink_windows_reparse_points(path)
            shutil.rmtree(path)
    except OSError as exc:
        return False, str(exc)
    return True, None


def _unlink_windows_reparse_points(root: Path) -> None:
    """Remove Linux-created link objects without following their targets."""

    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                attributes = getattr(metadata, "st_file_attributes", 0)
                if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                        os.rmdir(entry.path)
                    else:
                        os.unlink(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))


def _remove_volume(runner: str, name: str) -> bool:
    if not _VOLUME_NAME.fullmatch(name) or shutil.which(runner) is None:
        return False
    command = (
        [runner, "volume", "delete", name]
        if runner == "container"
        else [runner, "volume", "rm", name]
    )
    return run(command, check=False).returncode == 0


def cache_status() -> CacheReport:
    entries = _host_entries()
    evidence = _evidence_entries()
    volumes = [
        item
        for item in _managed_volumes()
        if item.get("runner") in {"docker", "podman", "container"}
        and isinstance(item.get("name"), str)
        and _VOLUME_NAME.fullmatch(item["name"])
    ]
    return CacheReport(
        host_bytes=sum(entry.size for entry in entries),
        host_entries=len(entries),
        prepared_volumes=len(volumes),
        evidence_bytes=sum(entry.size for entry in evidence) + _index_bytes(),
        evidence_entries=len(evidence),
    )


def _prune_cache_unlocked(
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
    max_evidence_entries: int = DEFAULT_MAX_EVIDENCE_ENTRIES,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_volumes: int = DEFAULT_MAX_VOLUMES,
    protected_path: Path | None = None,
    protected_evidence: Path | None = None,
    protected_volume: tuple[str, str] | None = None,
    protected_volumes: tuple[tuple[str, str], ...] = (),
) -> CacheReport:
    """Bound only cache entries and volumes created by gh-freshclone."""

    if min(
        max_bytes,
        max_entries,
        max_evidence_bytes,
        max_evidence_entries,
        max_age_days,
        max_volumes,
    ) < 0:
        raise ValueError("cache limits cannot be negative")
    now = time.time()
    cutoff = now - max_age_days * 24 * 60 * 60
    protected = protected_path.absolute() if protected_path else None
    entries = sorted(_host_entries(), key=lambda item: item.last_used)
    total = sum(entry.size for entry in entries)
    removed_bytes = 0
    removed_entries = 0
    remaining_count = len(entries)
    warnings: list[str] = []
    for entry in entries:
        if protected and entry.path.absolute() == protected:
            continue
        must_remove = (
            entry.last_used < cutoff
            or remaining_count > max_entries
            or total > max_bytes
        )
        if not must_remove:
            continue
        with cache_path_lock(entry.path):
            removed, error = _remove_host_entry(entry.path)
        if removed:
            total -= entry.size
            removed_bytes += entry.size
            removed_entries += 1
            remaining_count -= 1
        elif error:
            warnings.append(f"could not remove host cache {entry.path.name}: {error}")

    dangling_bytes, _, dangling_warnings = _prune_dangling_indexes()
    removed_evidence_bytes = dangling_bytes
    warnings.extend(dangling_warnings)
    evidence = sorted(_evidence_entries(), key=lambda item: item.last_used)
    index_bytes_by_receipt = _index_bytes_by_receipt()
    evidence_total = sum(entry.size for entry in evidence) + _index_bytes()
    removed_evidence_entries = 0
    remaining_evidence_count = len(evidence)
    protected_evidence_path = (
        protected_evidence.absolute() if protected_evidence else None
    )
    for entry in evidence:
        if protected_evidence_path and any(
            path.absolute() == protected_evidence_path for path in entry.files
        ):
            continue
        must_remove = (
            entry.stale
            or entry.last_used < cutoff
            or remaining_evidence_count > max_evidence_entries
            or evidence_total > max_evidence_bytes
        )
        if not must_remove:
            continue
        with ExitStack() as locks:
            for path in sorted(entry.files, key=_normalized_lock_path):
                locks.enter_context(cache_path_lock(path))
            removed, error = _remove_evidence_entry(entry)
        if removed:
            linked_index_bytes = (
                index_bytes_by_receipt.get(entry.files[0].resolve(), 0)
                if entry.files[0].suffix == ".json"
                else 0
            )
            evidence_total -= entry.size + linked_index_bytes
            removed_evidence_bytes += entry.size
            removed_evidence_entries += 1
            remaining_evidence_count -= 1
        if error:
            warnings.append(
                f"could not fully remove evidence {entry.files[0].name}: {error}"
            )
    dangling_bytes, _, dangling_warnings = _prune_dangling_indexes()
    removed_evidence_bytes += dangling_bytes
    warnings.extend(dangling_warnings)

    volumes = _managed_volumes()
    valid = [
        item
        for item in volumes
        if item.get("runner") in {"docker", "podman", "container"}
        and isinstance(item.get("name"), str)
        and _VOLUME_NAME.fullmatch(item["name"])
    ]
    valid.sort(key=lambda item: float(item.get("last_used_at", 0)))
    remove_keys: set[tuple[str, str]] = set()
    protected_volume_keys = set(protected_volumes)
    if protected_volume:
        protected_volume_keys.add(protected_volume)
    remaining_volumes = len(valid)
    for item in valid:
        key = (str(item["runner"]), str(item["name"]))
        if key in protected_volume_keys:
            continue
        last_used = float(item.get("last_used_at", 0))
        if last_used >= cutoff and remaining_volumes <= max_volumes:
            continue
        with cache_volume_lock(*key):
            if _remove_volume(*key):
                remove_keys.add(key)
                remaining_volumes -= 1
                _forget_prepared_volume(*key)
            elif shutil.which(key[0]) is None:
                warnings.append(
                    f"runner unavailable; retained volume record: {key[0]}:{key[1]}"
                )

    current = cache_status()
    return CacheReport(
        host_bytes=current.host_bytes,
        host_entries=current.host_entries,
        prepared_volumes=current.prepared_volumes,
        evidence_bytes=current.evidence_bytes,
        evidence_entries=current.evidence_entries,
        removed_bytes=removed_bytes,
        removed_entries=removed_entries,
        removed_evidence_bytes=removed_evidence_bytes,
        removed_evidence_entries=removed_evidence_entries,
        removed_volumes=len(remove_keys),
        warnings=tuple(warnings),
    )


def prune_cache(
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
    max_evidence_entries: int = DEFAULT_MAX_EVIDENCE_ENTRIES,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_volumes: int = DEFAULT_MAX_VOLUMES,
    protected_path: Path | None = None,
    protected_evidence: Path | None = None,
    protected_volume: tuple[str, str] | None = None,
    protected_volumes: tuple[tuple[str, str], ...] = (),
) -> CacheReport:
    """Bound app-owned caches while serializing registry mutations."""

    return _prune_cache_unlocked(
        max_bytes=max_bytes,
        max_entries=max_entries,
        max_evidence_bytes=max_evidence_bytes,
        max_evidence_entries=max_evidence_entries,
        max_age_days=max_age_days,
        max_volumes=max_volumes,
        protected_path=protected_path,
        protected_evidence=protected_evidence,
        protected_volume=protected_volume,
        protected_volumes=protected_volumes,
    )


def maybe_prune_cache(
    *,
    protected_path: Path | None = None,
    protected_evidence: Path | None = None,
    protected_volume: tuple[str, str] | None = None,
    protected_volumes: tuple[tuple[str, str], ...] = (),
) -> None:
    """Run bounded maintenance at most once per day; execution never depends on it."""

    marker = cache_root() / ".last-automatic-prune"
    try:
        if (
            marker.exists()
            and time.time() - marker.stat().st_mtime < AUTOMATIC_PRUNE_INTERVAL_SECONDS
        ):
            return
        prune_cache(
            protected_path=protected_path,
            protected_evidence=protected_evidence,
            protected_volume=protected_volume,
            protected_volumes=protected_volumes,
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except (OSError, ValueError):
        return
