from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
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
    DEFAULT_MAX_VOLUME_BYTES,
    DEFAULT_MAX_VOLUMES,
    DEFAULT_MIN_FREE_BYTES,
    RUNNER_CONTROL_TIMEOUT_SECONDS,
)
from .model import EXECUTION_POLICY_VERSION, RECEIPT_VERSION
from .process import run
from .receipts import cache_namespace, cache_root

_VOLUME_NAME = re.compile(
    r"ghfc-[a-f0-9]{8,12}-[a-f0-9]{12}-[A-Za-z0-9_.-]+$"
)
_HUMAN_SIZE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kmgtpe]?i?b)\Z",
    re.IGNORECASE,
)
_LAST_USED = ".gh-freshclone-last-used"
_CACHE_STATE_LOCK_KEY = "gh-freshclone-cache-state-v1"
_AUTOMATIC_PRUNE_LOCK_KEY = "gh-freshclone-automatic-prune-v1"


class CacheSpaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CacheReport:
    host_bytes: int
    host_entries: int
    prepared_volumes: int
    prepared_volume_bytes: int = 0
    prepared_volume_size_complete: bool = False
    evidence_bytes: int = 0
    evidence_entries: int = 0
    removed_bytes: int = 0
    removed_entries: int = 0
    removed_evidence_bytes: int = 0
    removed_evidence_entries: int = 0
    removed_volumes: int = 0
    removed_volume_bytes: int = 0
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


def _storage_status() -> list[tuple[Path, int]]:
    paths = (cache_root(), Path(tempfile.gettempdir()))
    status: list[tuple[Path, int]] = []
    devices: set[int] = set()
    for candidate in paths:
        path = candidate
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            device = path.stat().st_dev
            free = shutil.disk_usage(path).free
        except OSError:
            continue
        if device in devices:
            continue
        devices.add(device)
        status.append((path, free))
    return status


def ensure_storage_reserve(
    *,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> None:
    """Reclaim app cache before a fresh run can exhaust its host filesystem."""

    if min_free_bytes < 0:
        raise ValueError("minimum free-space reserve cannot be negative")
    status = _storage_status()
    deficits = [min_free_bytes - free for _, free in status if free < min_free_bytes]
    if not deficits:
        return
    reclaim = max(deficits) + 256 * 1024**2
    current_host_bytes = sum(entry.size for entry in _host_entries())
    report = prune_cache(max_bytes=max(0, current_host_bytes - reclaim))
    status = _storage_status()
    low = [(path, free) for path, free in status if free < min_free_bytes]
    if (
        low
        and report.prepared_volume_size_complete
        and report.prepared_volume_bytes
    ):
        remaining_reclaim = (
            max(min_free_bytes - free for _, free in low)
            + 256 * 1024**2
        )
        prune_cache(
            max_volume_bytes=max(
                0,
                report.prepared_volume_bytes - remaining_reclaim,
            )
        )
        status = _storage_status()
        low = [(path, free) for path, free in status if free < min_free_bytes]
    if not low:
        return
    path, free = min(low, key=lambda item: item[1])
    raise CacheSpaceError(
        "insufficient free space for a fresh baseline: "
        f"{path} has {free / 1024**3:.2f} GiB free; "
        f"{min_free_bytes / 1024**3:.2f} GiB is required after pruning "
        "app-owned cache"
    )


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
            timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
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


def _managed_volumes() -> list[dict[str, Any]]:
    # Keep the local ledger when a runner is stopped. A single label-filtered
    # discovery call recovers missing ledger entries; prune later forgets both
    # removed volumes and already-absent records without N per-volume probes.
    volumes = _read_registry()
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


def _parse_human_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(0, int(value))
    if not isinstance(value, str):
        return None
    match = _HUMAN_SIZE.fullmatch(value.strip())
    if match is None:
        return None
    unit = match.group("unit").lower()
    if unit == "b":
        multiplier = 1
    else:
        exponent = "kmgtpe".index(unit[0]) + 1
        multiplier = (1024 if "i" in unit else 1000) ** exponent
    return int(float(match.group("value")) * multiplier)


def _runner_volume_sizes(runner: str) -> dict[str, int] | None:
    """Read volume usage from runner metadata without mounting cache content."""

    if runner not in {"docker", "podman"} or shutil.which(runner) is None:
        return None
    command = [runner, "system", "df"]
    if runner == "docker":
        command.append("--verbose")
    command.extend(("--format", "json"))
    completed = run(
        command,
        check=False,
        timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    records = payload.get("Volumes", payload.get("volumes"))
    if not isinstance(records, list):
        return None
    sizes: dict[str, int] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        name = item.get("Name", item.get("name"))
        size = _parse_human_size(item.get("Size", item.get("size")))
        if isinstance(name, str) and size is not None:
            sizes[name] = size
    return sizes


def _prepared_volume_sizes(
    volumes: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], int], bool]:
    sizes: dict[tuple[str, str], int] = {}
    complete = True
    runners = {
        str(item["runner"])
        for item in volumes
        if isinstance(item.get("runner"), str)
    }
    runner_sizes = {
        runner: _runner_volume_sizes(runner)
        for runner in runners
    }
    for item in volumes:
        key = (str(item.get("runner")), str(item.get("name")))
        available = runner_sizes.get(key[0])
        if available is None or key[1] not in available:
            complete = False
            continue
        sizes[key] = available[key[1]]
    return sizes, complete


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
            if path.is_file():
                path.unlink()
                return True, None
            removal_path = path
            if os.name == "nt":
                removal_path = _windows_extended_path(path)
                _unlink_windows_reparse_points(removal_path)
            for attempt in range(3):
                try:
                    shutil.rmtree(removal_path, onerror=_rmtree_onerror)
                    break
                except FileNotFoundError:
                    break
                except OSError as exc:
                    directory_not_empty = (
                        exc.errno == errno.ENOTEMPTY
                        or getattr(exc, "winerror", None) == 145
                    )
                    if not directory_not_empty or attempt == 2:
                        raise
    except OSError as exc:
        return False, str(exc)
    return True, None


def _windows_extended_path(path: Path) -> Path:
    """Use a Win32 extended path only after the normal path passed root checks."""

    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _rmtree_onexc(
    function: Any,
    path: str,
    error: BaseException,
) -> None:
    """Retry read-only cache nodes and tolerate paths removed by a prior retry."""

    if isinstance(error, FileNotFoundError):
        return
    if not isinstance(error, PermissionError):
        raise error
    try:
        os.chmod(path, stat.S_IRWXU)
        function(path)
    except FileNotFoundError:
        return


def _rmtree_onerror(
    function: Any,
    path: str,
    error_info: tuple[type[BaseException], BaseException, Any],
) -> None:
    """Python 3.11-compatible adapter for the cache cleanup retry."""

    _rmtree_onexc(function, path, error_info[1])


def _unlink_windows_reparse_points(root: Path) -> None:
    """Remove Linux-created link objects without following their targets."""

    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        attributes = getattr(metadata, "st_file_attributes", 0)
                        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                                os.rmdir(entry.path)
                            else:
                                os.unlink(entry.path)
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue


def _remove_volume(runner: str, name: str) -> bool:
    if not _VOLUME_NAME.fullmatch(name) or shutil.which(runner) is None:
        return False
    command = (
        [runner, "volume", "delete", name]
        if runner == "container"
        else [runner, "volume", "rm", name]
    )
    completed = run(
        command,
        check=False,
        timeout=RUNNER_CONTROL_TIMEOUT_SECONDS,
    )
    if completed.returncode == 0:
        return True
    detail = (completed.stderr or completed.stdout).lower()
    return any(
        marker in detail
        for marker in (
            "no such volume",
            "volume not found",
            "does not exist",
        )
    )


def discard_dependency_cache(path: Path) -> tuple[bool, str | None]:
    """Discard one already-locked app cache after failed preparation."""

    return _remove_host_entry(path)


def discard_prepared_volume(runner: str, name: str) -> tuple[bool, str | None]:
    """Discard one already-locked prepared volume and its ledger entry."""

    if not _remove_volume(runner, name):
        return False, "runner rejected removal"
    _forget_prepared_volume(runner, name)
    return True, None


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
    volume_sizes, size_complete = _prepared_volume_sizes(volumes)
    return CacheReport(
        host_bytes=sum(entry.size for entry in entries),
        host_entries=len(entries),
        prepared_volumes=len(volumes),
        prepared_volume_bytes=sum(volume_sizes.values()),
        prepared_volume_size_complete=size_complete,
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
    max_volume_bytes: int = DEFAULT_MAX_VOLUME_BYTES,
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
        max_volume_bytes,
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
    volume_sizes, _ = _prepared_volume_sizes(valid)
    remaining_volume_bytes = sum(volume_sizes.values())
    remove_keys: set[tuple[str, str]] = set()
    removed_volume_bytes = 0
    failed_volume_removals: dict[tuple[str, str], list[str]] = {}
    protected_volume_keys = set(protected_volumes)
    if protected_volume:
        protected_volume_keys.add(protected_volume)
    remaining_volumes = len(valid)
    for item in valid:
        key = (str(item["runner"]), str(item["name"]))
        if key in protected_volume_keys:
            continue
        last_used = float(item.get("last_used_at", 0))
        volume_size = volume_sizes.get(key)
        exceeds_byte_limit = (
            volume_size is not None
            and remaining_volume_bytes > max_volume_bytes
        )
        if (
            last_used >= cutoff
            and remaining_volumes <= max_volumes
            and not exceeds_byte_limit
        ):
            continue
        with cache_volume_lock(*key):
            if _remove_volume(*key):
                remove_keys.add(key)
                remaining_volumes -= 1
                if volume_size is not None:
                    remaining_volume_bytes -= volume_size
                    removed_volume_bytes += volume_size
                _forget_prepared_volume(*key)
            else:
                reason = (
                    "runner unavailable"
                    if shutil.which(key[0]) is None
                    else "runner rejected removal"
                )
                failed_volume_removals.setdefault((reason, key[0]), []).append(key[1])

    for (reason, runner), names in sorted(failed_volume_removals.items()):
        if len(names) == 1:
            retained = f"volume record: {runner}:{names[0]}"
        else:
            retained = f"{len(names)} managed volume records for {runner}"
        warnings.append(f"{reason}; retained {retained}")

    current = cache_status()
    return CacheReport(
        host_bytes=current.host_bytes,
        host_entries=current.host_entries,
        prepared_volumes=current.prepared_volumes,
        prepared_volume_bytes=current.prepared_volume_bytes,
        prepared_volume_size_complete=current.prepared_volume_size_complete,
        evidence_bytes=current.evidence_bytes,
        evidence_entries=current.evidence_entries,
        removed_bytes=removed_bytes,
        removed_entries=removed_entries,
        removed_evidence_bytes=removed_evidence_bytes,
        removed_evidence_entries=removed_evidence_entries,
        removed_volumes=len(remove_keys),
        removed_volume_bytes=removed_volume_bytes,
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
    max_volume_bytes: int = DEFAULT_MAX_VOLUME_BYTES,
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
        max_volume_bytes=max_volume_bytes,
        protected_path=protected_path,
        protected_evidence=protected_evidence,
        protected_volume=protected_volume,
        protected_volumes=protected_volumes,
    )


def _prepared_volume_limits_exceeded() -> bool:
    """Check growth-prone prepared volumes without walking host cache trees."""

    volumes = [
        item
        for item in _managed_volumes()
        if item.get("runner") in {"docker", "podman", "container"}
        and isinstance(item.get("name"), str)
        and _VOLUME_NAME.fullmatch(item["name"])
    ]
    if len(volumes) > DEFAULT_MAX_VOLUMES:
        return True
    volume_sizes, size_complete = _prepared_volume_sizes(volumes)
    return (
        size_complete
        and sum(volume_sizes.values()) > DEFAULT_MAX_VOLUME_BYTES
    )


def _host_cache_limits_exceeded() -> bool:
    entries = _host_entries()
    return (
        len(entries) > DEFAULT_MAX_ENTRIES
        or sum(entry.size for entry in entries) > DEFAULT_MAX_BYTES
    )


def maybe_prune_cache(
    *,
    prepared_volumes_changed: bool = False,
    host_cache_changed: bool = False,
    protected_path: Path | None = None,
    protected_evidence: Path | None = None,
    protected_volume: tuple[str, str] | None = None,
    protected_volumes: tuple[tuple[str, str], ...] = (),
) -> None:
    """Run daily maintenance, or reclaim a newly observed cache overflow."""

    marker = cache_root() / ".last-automatic-prune"
    try:
        with cache_lock(_AUTOMATIC_PRUNE_LOCK_KEY):
            recently_pruned = (
                marker.exists()
                and time.time() - marker.stat().st_mtime
                < AUTOMATIC_PRUNE_INTERVAL_SECONDS
            )
            volume_reclaim_required = (
                prepared_volumes_changed
                and _prepared_volume_limits_exceeded()
            )
            host_reclaim_required = (
                host_cache_changed
                and _host_cache_limits_exceeded()
            )
            if (
                recently_pruned
                and not volume_reclaim_required
                and not host_reclaim_required
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
