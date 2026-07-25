from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gh_freshclone import cache as cache_module
from gh_freshclone.model import Receipt
from gh_freshclone.workflow import check_repository

E2E_RUNNER = os.environ.get("GH_FRESHCLONE_E2E_RUNNER", "")

pytestmark = pytest.mark.skipif(
    E2E_RUNNER not in {"docker", "podman", "container"},
    reason="set GH_FRESHCLONE_E2E_RUNNER to opt into a native runner E2E",
)


def _cleanup_test_volumes() -> None:
    for item in cache_module._read_registry():
        runner = item.get("runner")
        name = item.get("name")
        if isinstance(runner, str) and isinstance(name, str):
            cache_module._remove_volume(runner, name)


def test_native_runner_executes_prepare_then_offline_test(
    monkeypatch: pytest.MonkeyPatch,
    git_repository: Path,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "e2e-cache"
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(cache))
    try:
        receipt, _, cached = check_repository(
            str(git_repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=False,
        )

        assert isinstance(receipt, Receipt)
        assert cached is False
        assert receipt.status == "pass"
        assert receipt.runner == E2E_RUNNER
        assert receipt.resource_limits.to_dict() == {
            "cpus": 4.0,
            "memory": "8g",
        }
        assert len(receipt.results) == 1
        result = receipt.results[0]
        assert result.status == "pass"
        assert result.prepare_duration_seconds > 0
        assert result.test_network == "none"
        assert result.failed_phase is None
        assert result.prepared_volume
        log = Path(result.log_path).read_text(encoding="utf-8")
        assert "prepare (network enabled) phase" in log
        assert "test (network none) phase" in log
    finally:
        _cleanup_test_volumes()


def test_native_runner_persists_pnpm_and_modules_across_phases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "node-repository"
    repository.mkdir()
    application = repository / "apps" / "web"
    application.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    (application / "package.json").write_text(
        json.dumps(
            {
                "name": "freshclone-node-e2e",
                "version": "1.0.0",
                "private": True,
                "packageManager": "pnpm@10.13.1",
                "dependencies": {"is-number": "7.0.0"},
                "scripts": {"test": "node --test"},
            }
        ),
        encoding="utf-8",
    )
    (application / "pnpm-lock.yaml").write_text(
        """
lockfileVersion: '9.0'

settings:
  autoInstallPeers: true
  excludeLinksFromLockfile: false

importers:

  .:
    dependencies:
      is-number:
        specifier: 7.0.0
        version: 7.0.0

packages:

  is-number@7.0.0:
    resolution: {integrity: sha512-41Cifkg6e8TylSpdtTpeLVMqvSBEVzTttHvERD741+pnZ8ANv0004MRL43QKPDlK9cGvNp6NZWZUBlbGXYxxng==}
    engines: {node: '>=0.12.0'}

snapshots:

  is-number@7.0.0: {}
""".lstrip(),
        encoding="utf-8",
    )
    (application / "baseline.test.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import isNumber from 'is-number';\n"
        "test('baseline', () => {\n"
        "  assert.equal(2 + 2, 4);\n"
        "  assert.equal(isNumber('42'), true);\n"
        "});\n",
        encoding="utf-8",
    )
    (repository / ".gh-freshclone.toml").write_text(
        """
version = 1

[[steps]]
path = "apps/web"
ecosystem = "node"
image = "docker.io/library/node:24-bookworm"
prepare_command = "npm install --prefix /prepared/corepack --no-audit --no-fund corepack@0.34.0 && /prepared/corepack/node_modules/.bin/corepack pnpm install --frozen-lockfile"
command = "/prepared/corepack/node_modules/.bin/corepack pnpm run test"
test_network = "none"
dependency_files = ["package.json", "pnpm-lock.yaml"]
""".lstrip(),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "."],
        check=True,
        capture_output=True,
    )
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
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "node-e2e-cache"))
    try:
        receipt, _, _ = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=False,
        )

        assert isinstance(receipt, Receipt)
        assert receipt.status == "pass", receipt.to_dict()
        assert receipt.resource_limits.to_dict() == {
            "cpus": 4.0,
            "memory": "8g",
        }
        assert receipt.results[0].test_network == "none"
        assert receipt.results[0].prepared_volume
        assert receipt.results[0].prepare_cache_hit is False
        assert receipt.plan.steps[0].working_directory == "apps/web"

        repeated, _, repeated_cached = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=False,
        )

        assert isinstance(repeated, Receipt)
        assert repeated.status == "pass", repeated.to_dict()
        assert repeated_cached is False
        assert repeated.results[0].prepare_cache_hit is True
    finally:
        _cleanup_test_volumes()
