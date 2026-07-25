from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from gh_freshclone.cache import (
    _unlink_windows_reparse_points,
    cache_lock,
    cache_path_lock,
    cache_status,
    cache_volume_lock,
    prune_cache,
    touch_dependency_cache,
)
from gh_freshclone.model import EXECUTION_POLICY_VERSION, RECEIPT_VERSION


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

    report = prune_cache(
        max_bytes=1024,
        max_entries=1,
        max_age_days=30,
        max_volumes=24,
    )

    assert report.removed_volumes == 0


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
    name = "ghfc-12345678-123456789abc-quick-python-cache-p6-e15"
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
