from __future__ import annotations

import errno
import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from gh_freshclone.cache import (
    CacheReport,
    CacheSpaceError,
    _parse_human_size,
    _remove_host_entry,
    _remove_volume,
    _rmtree_onexc,
    _unlink_windows_reparse_points,
    _windows_extended_path,
    cache_lock,
    cache_path_lock,
    cache_status,
    cache_volume_lock,
    ensure_storage_reserve,
    maybe_prune_cache,
    prune_cache,
    touch_dependency_cache,
)
from gh_freshclone.model import EXECUTION_POLICY_VERSION, RECEIPT_VERSION
from gh_freshclone.process import Completed


def _entry(root: Path, fingerprint: str, *, size: int, age: int) -> Path:
    path = root / "runner-cache" / "docker" / "owner_repo" / "python" / fingerprint
    touch_dependency_cache(path)
    (path / "payload").write_bytes(b"x" * size)
    timestamp = 1_700_000_000 + age
    os.utime(path / ".gh-freshclone-last-used", (timestamp, timestamp))
    return path


def _evidence(
    root: Path,
    name: str,
    *,
    size: int,
    age: int,
    indexed: bool = False,
    execution_policy_version: int = EXECUTION_POLICY_VERSION,
) -> tuple[Path, Path, Path | None]:
    receipt_dir = root / "receipts" / "owner_repo"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f"{name}.json"
    log = receipt_dir / f"{name}-1-python.log"
    receipt.write_text(
        json.dumps(
            {
                "receipt_version": RECEIPT_VERSION,
                "execution_policy_version": execution_policy_version,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log.write_bytes(b"x" * size)
    timestamp = 1_700_000_000 + age
    os.utime(receipt, (timestamp, timestamp))
    os.utime(log, (timestamp, timestamp))
    index = None
    if indexed:
        index_dir = root / "indexes" / "owner_repo"
        index_dir.mkdir(parents=True, exist_ok=True)
        index = index_dir / f"{name}.json"
        index.write_text(
            json.dumps(
                {"receipt": f"receipts/owner_repo/{receipt.name}"},
            ),
            encoding="utf-8",
        )
    return receipt, log, index


def test_prune_removes_only_oldest_app_cache_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    old = _entry(tmp_path, "old", size=8, age=0)
    recent = _entry(tmp_path, "recent", size=8, age=100)

    report = prune_cache(
        max_bytes=1024,
        max_entries=1,
        max_age_days=100_000,
        max_volumes=24,
        protected_path=recent,
    )

    assert not old.exists()
    assert recent.exists()
    assert report.removed_entries == 1
    assert report.host_entries == 1


def test_cache_status_counts_dependency_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    _entry(tmp_path, "one", size=7, age=0)

    report = cache_status()

    assert report.host_entries == 1
    assert report.host_bytes >= 7


def test_cache_status_counts_receipt_log_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    _evidence(tmp_path, "proof", size=7, age=0, indexed=True)

    report = cache_status()

    assert report.evidence_entries == 1
    assert report.evidence_bytes >= 7


def test_recent_automatic_prune_skips_unchanged_prepared_volumes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    monkeypatch.setattr(
        "gh_freshclone.cache._prepared_volume_limits_exceeded",
        lambda: pytest.fail("unchanged volumes should not be measured"),
    )
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: pytest.fail("recent maintenance should be reused"),
    )

    maybe_prune_cache()


def test_recent_automatic_prune_reclaims_prepared_volume_overflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    names = [
        "ghfc-12345678-123456789abc-quick-python-old-p6-e16",
        "ghfc-12345678-123456789abc-quick-python-new-p6-e16",
    ]
    monkeypatch.setattr(
        "gh_freshclone.cache._managed_volumes",
        lambda: [
            {
                "runner": "docker",
                "name": name,
                "created_at": 1_900_000_000,
                "last_used_at": 1_900_000_000,
            }
            for name in names
        ],
    )
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: dict.fromkeys(names, 100),
    )
    monkeypatch.setattr("gh_freshclone.cache.DEFAULT_MAX_VOLUME_BYTES", 150)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: calls.append(kwargs),
    )

    maybe_prune_cache(prepared_volumes_changed=True)

    assert calls == [
        {
            "protected_path": None,
            "protected_evidence": None,
            "protected_volume": None,
            "protected_volumes": (),
        }
    ]
    assert marker.stat().st_mtime != 1_900_000_000


def test_recent_automatic_prune_reclaims_host_cache_overflow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    monkeypatch.setattr(
        "gh_freshclone.cache._host_cache_limits_exceeded",
        lambda: True,
    )
    monkeypatch.setattr(
        "gh_freshclone.cache._prepared_volume_limits_exceeded",
        lambda: pytest.fail("unchanged volumes should not be measured"),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: calls.append(kwargs),
    )

    maybe_prune_cache(host_cache_changed=True)

    assert len(calls) == 1


def test_recent_automatic_prune_keeps_changed_host_cache_below_limits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    monkeypatch.setattr(
        "gh_freshclone.cache._host_cache_limits_exceeded",
        lambda: False,
    )
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: pytest.fail("host cache remains within hard limits"),
    )

    maybe_prune_cache(host_cache_changed=True)


def test_recent_automatic_prune_keeps_changed_volumes_below_limits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    name = "ghfc-12345678-123456789abc-quick-python-cache-p6-e16"
    monkeypatch.setattr(
        "gh_freshclone.cache._managed_volumes",
        lambda: [
            {
                "runner": "docker",
                "name": name,
                "created_at": 1_900_000_000,
                "last_used_at": 1_900_000_000,
            }
        ],
    )
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: {name: 100},
    )
    monkeypatch.setattr("gh_freshclone.cache.DEFAULT_MAX_VOLUME_BYTES", 150)
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: pytest.fail("cache remains within its hard limits"),
    )

    maybe_prune_cache(prepared_volumes_changed=True)


def test_recent_automatic_prune_enforces_count_without_byte_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    names = [
        "ghfc-12345678-123456789abc-quick-python-old-p6-e16",
        "ghfc-12345678-123456789abc-quick-python-new-p6-e16",
    ]
    monkeypatch.setattr(
        "gh_freshclone.cache._managed_volumes",
        lambda: [
            {
                "runner": "container",
                "name": name,
                "created_at": 1_900_000_000,
                "last_used_at": 1_900_000_000,
            }
            for name in names
        ],
    )
    monkeypatch.setattr("gh_freshclone.cache.DEFAULT_MAX_VOLUMES", 1)
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: pytest.fail("count overflow needs no byte metadata"),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: calls.append(kwargs),
    )

    maybe_prune_cache(prepared_volumes_changed=True)

    assert len(calls) == 1


def test_expired_automatic_prune_runs_without_volume_growth_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    marker = tmp_path / ".last-automatic-prune"
    marker.touch()
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_100_000)
    os.utime(marker, (1_900_000_000, 1_900_000_000))
    monkeypatch.setattr(
        "gh_freshclone.cache._prepared_volume_limits_exceeded",
        lambda: pytest.fail("daily maintenance does not need a pre-scan"),
    )
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: calls.append(kwargs),
    )

    maybe_prune_cache()

    assert len(calls) == 1


def test_runner_volume_size_units_are_normalized_to_bytes() -> None:
    assert _parse_human_size("0B") == 0
    assert _parse_human_size("1.5MB") == 1_500_000
    assert _parse_human_size("2 GiB") == 2 * 1024**3
    assert _parse_human_size("N/A") is None


def test_low_storage_prunes_app_cache_before_fresh_execution(
    monkeypatch,
) -> None:
    gib = 1024**3
    readings = iter(
        [
            [(Path("cache"), gib)],
            [(Path("cache"), 3 * gib)],
        ]
    )
    observed: dict[str, int] = {}
    monkeypatch.setattr(
        "gh_freshclone.cache._storage_status",
        lambda: next(readings),
    )
    monkeypatch.setattr(
        "gh_freshclone.cache._host_entries",
        lambda: [SimpleNamespace(size=3 * gib)],
    )
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: (
            observed.update(kwargs)
            or CacheReport(
                host_bytes=0,
                host_entries=0,
                prepared_volumes=0,
            )
        ),
    )

    ensure_storage_reserve(min_free_bytes=2 * gib)

    assert observed["max_bytes"] == int(1.75 * gib)


def test_low_storage_tightens_measured_volume_budget_when_host_prune_is_not_enough(
    monkeypatch,
) -> None:
    gib = 1024**3
    readings = iter(
        [
            [(Path("cache"), gib)],
            [(Path("cache"), gib)],
            [(Path("cache"), 3 * gib)],
        ]
    )
    calls: list[dict[str, int]] = []

    monkeypatch.setattr(
        "gh_freshclone.cache._storage_status",
        lambda: next(readings),
    )
    monkeypatch.setattr("gh_freshclone.cache._host_entries", list)

    def prune(**kwargs):
        calls.append(kwargs)
        return CacheReport(
            host_bytes=0,
            host_entries=0,
            prepared_volumes=3,
            prepared_volume_bytes=3 * gib,
            prepared_volume_size_complete=True,
        )

    monkeypatch.setattr("gh_freshclone.cache.prune_cache", prune)

    ensure_storage_reserve(min_free_bytes=2 * gib)

    assert calls[0]["max_bytes"] == 0
    assert calls[1]["max_volume_bytes"] == int(1.75 * gib)


def test_low_storage_fails_before_fresh_execution_when_prune_cannot_recover(
    monkeypatch,
) -> None:
    gib = 1024**3
    monkeypatch.setattr(
        "gh_freshclone.cache._storage_status",
        lambda: [(Path("cache"), gib)],
    )
    monkeypatch.setattr("gh_freshclone.cache._host_entries", list)
    monkeypatch.setattr(
        "gh_freshclone.cache.prune_cache",
        lambda **kwargs: CacheReport(
            host_bytes=0,
            host_entries=0,
            prepared_volumes=0,
        ),
    )

    with pytest.raises(CacheSpaceError, match="1.00 GiB free"):
        ensure_storage_reserve(min_free_bytes=2 * gib)


def test_rmtree_retry_makes_read_only_cache_node_writable(
    monkeypatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(
        "gh_freshclone.cache.os.chmod",
        lambda path, mode: calls.append(("chmod", path, mode)),
    )

    def remove(path: str) -> None:
        calls.append(("remove", path))

    _rmtree_onexc(remove, "cache-node", PermissionError("read only"))

    assert calls == [
        ("chmod", "cache-node", stat.S_IRWXU),
        ("remove", "cache-node"),
    ]


def test_host_cache_cleanup_retries_directory_not_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    entry = _entry(tmp_path, "retry", size=8, age=0)
    real_rmtree = __import__("shutil").rmtree
    attempts = 0

    def flaky_rmtree(path, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(errno.ENOTEMPTY, "directory not empty")
        return real_rmtree(path, **kwargs)

    monkeypatch.setattr("gh_freshclone.cache.shutil.rmtree", flaky_rmtree)

    removed, error = _remove_host_entry(entry)

    assert removed is True
    assert error is None
    assert attempts == 2


def test_host_cache_cleanup_discards_corrupt_regular_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    entry = tmp_path / "runner-cache" / "source" / "owner" / "component" / "commit"
    entry.parent.mkdir(parents=True)
    entry.write_text("corrupt cache boundary\n", encoding="utf-8")

    removed, error = _remove_host_entry(entry)

    assert removed is True
    assert error is None
    assert not entry.exists()


def test_prune_bounds_evidence_and_removes_its_pass_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    old_receipt, old_log, old_index = _evidence(
        tmp_path,
        "old",
        size=8,
        age=0,
        indexed=True,
    )
    recent_receipt, recent_log, _ = _evidence(
        tmp_path,
        "recent",
        size=8,
        age=100,
    )

    report = prune_cache(
        max_bytes=1024,
        max_entries=128,
        max_evidence_bytes=1024,
        max_evidence_entries=1,
        max_age_days=100_000,
        max_volumes=24,
        protected_evidence=recent_log,
    )

    assert not old_receipt.exists()
    assert not old_log.exists()
    assert old_index is not None and not old_index.exists()
    assert recent_receipt.exists()
    assert recent_log.exists()
    assert report.removed_evidence_entries == 1
    assert report.evidence_entries == 1


def test_prune_recovers_orphan_log_and_malformed_index(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    receipt_dir = tmp_path / "receipts" / "owner_repo"
    receipt_dir.mkdir(parents=True)
    orphan = receipt_dir / "interrupted-1-python.log"
    orphan.write_text("partial", encoding="utf-8")
    index_dir = tmp_path / "indexes" / "owner_repo"
    index_dir.mkdir(parents=True)
    malformed = index_dir / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    report = prune_cache(
        max_bytes=1024,
        max_entries=128,
        max_evidence_bytes=0,
        max_evidence_entries=0,
        max_age_days=100_000,
        max_volumes=24,
    )

    assert not orphan.exists()
    assert not malformed.exists()
    assert report.removed_evidence_entries == 1
    assert report.evidence_entries == 0


def test_prune_removes_incompatible_execution_policy_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    receipt, log, index = _evidence(
        tmp_path,
        "stale-policy",
        size=8,
        age=100,
        indexed=True,
        execution_policy_version=EXECUTION_POLICY_VERSION - 1,
    )

    report = prune_cache(
        max_bytes=1024,
        max_entries=128,
        max_evidence_bytes=1024,
        max_evidence_entries=512,
        max_age_days=100_000,
        max_volumes=24,
    )

    assert not receipt.exists()
    assert not log.exists()
    assert index is not None and not index.exists()
    assert report.removed_evidence_entries == 1


def test_cache_status_recovers_labeled_volume_without_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: {},
    )
    monkeypatch.setattr(
        "gh_freshclone.cache._discover_managed_volumes",
        lambda: [
            {
                "runner": "docker",
                "name": "ghfc-12345678-123456789abc-quick-python-cache-p3-e5",
                "created_at": 0,
                "last_used_at": 0,
            }
        ],
    )

    assert cache_status().prepared_volumes == 1


def test_recovered_volume_is_not_treated_as_immediately_expired(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    monkeypatch.setattr(
        "gh_freshclone.cache._discover_managed_volumes",
        lambda: [
            {
                "runner": "docker",
                "name": "ghfc-12345678-123456789abc-quick-python-cache-p5-e9",
                "created_at": 1_900_000_000,
                "last_used_at": 1_900_000_000,
            }
        ],
    )
    monkeypatch.setattr("gh_freshclone.cache._remove_volume", lambda *args: True)
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: {},
    )

    report = prune_cache(
        max_bytes=1024,
        max_entries=1,
        max_age_days=30,
        max_volumes=24,
    )

    assert report.removed_volumes == 0


def test_prune_bounds_prepared_volumes_by_observed_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache.time.time", lambda: 1_900_000_100)
    oldest = "ghfc-12345678-123456789abc-quick-python-old-p6-e16"
    recent = "ghfc-12345678-123456789abc-quick-python-new-p6-e16"
    present = {oldest, recent}

    def managed() -> list[dict[str, object]]:
        return [
            {
                "runner": "docker",
                "name": name,
                "created_at": 1_900_000_000,
                "last_used_at": 1_900_000_000 + index,
            }
            for index, name in enumerate((oldest, recent))
            if name in present
        ]

    def remove(_runner: str, name: str) -> bool:
        present.remove(name)
        return True

    monkeypatch.setattr("gh_freshclone.cache._managed_volumes", managed)
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: {oldest: 100, recent: 100},
    )
    monkeypatch.setattr("gh_freshclone.cache._remove_volume", remove)

    report = prune_cache(
        max_bytes=1024,
        max_entries=128,
        max_evidence_bytes=1024,
        max_evidence_entries=512,
        max_age_days=30,
        max_volumes=24,
        max_volume_bytes=150,
    )

    assert present == {recent}
    assert report.prepared_volumes == 1
    assert report.prepared_volume_bytes == 100
    assert report.prepared_volume_size_complete is True
    assert report.removed_volumes == 1
    assert report.removed_volume_bytes == 100


def test_prune_reports_runner_volume_removal_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    name = "ghfc-12345678-123456789abc-quick-python-cache-p6-e16"
    monkeypatch.setattr(
        "gh_freshclone.cache._managed_volumes",
        lambda: [
            {
                "runner": "docker",
                "name": name,
                "created_at": 0,
                "last_used_at": 0,
            }
        ],
    )
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: {name: 100},
    )
    monkeypatch.setattr("gh_freshclone.cache._remove_volume", lambda *args: False)
    monkeypatch.setattr("gh_freshclone.cache.shutil.which", lambda runner: runner)

    report = prune_cache(
        max_bytes=1024,
        max_entries=128,
        max_evidence_bytes=1024,
        max_evidence_entries=512,
        max_age_days=100_000,
        max_volumes=0,
    )

    assert report.removed_volumes == 0
    assert report.warnings == (
        f"runner rejected removal; retained volume record: docker:{name}",
    )


def test_missing_runner_volume_is_successful_ledger_cleanup(
    monkeypatch,
) -> None:
    name = "ghfc-12345678-123456789abc-quick-python-cache-p6-e16"
    monkeypatch.setattr("gh_freshclone.cache.shutil.which", lambda runner: runner)
    monkeypatch.setattr(
        "gh_freshclone.cache.run",
        lambda *args, **kwargs: Completed(
            tuple(args[0]),
            1,
            "",
            f"Error: no such volume: {name}",
        ),
    )

    assert _remove_volume("docker", name) is True


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point behavior")
def test_windows_reparse_cleanup_never_follows_link_target(tmp_path: Path) -> None:
    cache_entry = tmp_path / "cache-entry"
    cache_entry.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    link = cache_entry / "linux-cache-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    _unlink_windows_reparse_points(cache_entry)

    assert not link.exists()
    assert protected.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-path behavior")
def test_windows_cache_removal_uses_extended_absolute_path(tmp_path: Path) -> None:
    extended = str(_windows_extended_path(tmp_path / "cache"))

    assert extended.startswith("\\\\?\\")
    assert extended.endswith("\\cache")


def test_reparse_cleanup_tolerates_disappearing_cache_node(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "cache-entry"
    root.mkdir()
    real_scandir = os.scandir

    def disappearing(path):
        if Path(path) == root:
            raise FileNotFoundError(path)
        return real_scandir(path)

    monkeypatch.setattr("gh_freshclone.cache.os.scandir", disappearing)

    _unlink_windows_reparse_points(root)


def test_matching_cache_locks_serialize_threads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    waiting = threading.Event()
    entered = threading.Event()

    def contender() -> None:
        waiting.set()
        with cache_lock("same-probe"):
            entered.set()

    with cache_lock("same-probe"):
        thread = threading.Thread(target=contender)
        thread.start()
        assert waiting.wait(timeout=1)
        assert not entered.wait(timeout=0.15)

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert entered.is_set()


def test_prune_waits_for_in_use_host_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    entry = _entry(tmp_path, "active", size=8, age=0)
    started = threading.Event()
    finished = threading.Event()

    def prune() -> None:
        started.set()
        prune_cache(
            max_bytes=0,
            max_entries=0,
            max_evidence_bytes=1024,
            max_evidence_entries=512,
            max_age_days=100_000,
            max_volumes=24,
        )
        finished.set()

    with cache_path_lock(entry):
        thread = threading.Thread(target=prune)
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.15)
        assert entry.exists()

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert finished.is_set()
    assert not entry.exists()


def test_prune_waits_for_in_use_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    monkeypatch.setattr("gh_freshclone.cache._discover_managed_volumes", list)
    receipt, log, _ = _evidence(tmp_path, "active", size=8, age=0)
    started = threading.Event()
    finished = threading.Event()

    def prune() -> None:
        started.set()
        prune_cache(
            max_bytes=1024,
            max_entries=128,
            max_evidence_bytes=0,
            max_evidence_entries=0,
            max_age_days=100_000,
            max_volumes=24,
        )
        finished.set()

    with cache_path_lock(log):
        thread = threading.Thread(target=prune)
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.15)
        assert receipt.exists()
        assert log.exists()

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert finished.is_set()
    assert not receipt.exists()
    assert not log.exists()


def test_prune_waits_for_in_use_prepared_volume(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path))
    name = "ghfc-12345678-123456789abc-quick-python-cache-p6-e16"
    state = {"present": True}
    started = threading.Event()
    finished = threading.Event()

    def managed() -> list[dict[str, object]]:
        return (
            [
                {
                    "runner": "docker",
                    "name": name,
                    "created_at": 0,
                    "last_used_at": 0,
                }
            ]
            if state["present"]
            else []
        )

    def remove(runner: str, volume: str) -> bool:
        assert (runner, volume) == ("docker", name)
        state["present"] = False
        return True

    monkeypatch.setattr("gh_freshclone.cache._managed_volumes", managed)
    monkeypatch.setattr(
        "gh_freshclone.cache._runner_volume_sizes",
        lambda runner: {name: 8},
    )
    monkeypatch.setattr("gh_freshclone.cache._remove_volume", remove)

    def prune() -> None:
        started.set()
        prune_cache(
            max_bytes=1024,
            max_entries=128,
            max_evidence_bytes=1024,
            max_evidence_entries=512,
            max_age_days=100_000,
            max_volumes=0,
        )
        finished.set()

    with cache_volume_lock("docker", name):
        thread = threading.Thread(target=prune)
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.15)
        assert state["present"] is True

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert finished.is_set()
    assert state["present"] is False
