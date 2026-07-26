from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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
    repository = tmp_path / "repository with spaces_日本語"
    git_repository.rename(repository)
    boundary_test = repository / "tests" / "test_runner_boundary.py"
    boundary_source = """
import platform
from pathlib import Path

EXPECTED_ARCH = None


def test_network_is_absent_and_input_is_read_only():
    interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
    if EXPECTED_ARCH is not None:
        assert interfaces == {"lo"}
    ipv4_routes = [
        line.split()
        for line in Path("/proc/net/route").read_text().splitlines()[1:]
    ]
    assert not any(columns[1] == "00000000" for columns in ipv4_routes)
    ipv6_routes = [
        line.split()
        for line in Path("/proc/net/ipv6_route").read_text().splitlines()
    ]
    ipv6_defaults = [
        columns
        for columns in ipv6_routes
        if columns[0] == "0" * 32 and columns[1] == "00"
    ]
    assert all(
        columns[5].lower() == "ffffffff" and columns[-1] == "lo"
        for columns in ipv6_defaults
    )
    tmp_mount = next(
        line
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
        if " /tmp " in line
    )
    assert " - tmpfs " in tmp_mount
    try:
        Path("/input/gh-freshclone-write-probe").write_text("must fail")
    except OSError:
        pass
    else:
        raise AssertionError("/input must be read-only")


def test_guest_architecture():
    if EXPECTED_ARCH is not None:
        assert platform.machine() == EXPECTED_ARCH
""".lstrip()
    if E2E_RUNNER == "container":
        boundary_source = boundary_source.replace(
            "EXPECTED_ARCH = None",
            'EXPECTED_ARCH = "aarch64"',
        )
    boundary_test.write_text(
        boundary_source,
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", str(boundary_test.relative_to(repository))],
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
            "add runner boundary probe",
        ],
        check=True,
        capture_output=True,
    )
    if E2E_RUNNER == "container":
        runner_temp = tmp_path / "Apple container temp_日本語"
        runner_temp.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(runner_temp))
    cache = tmp_path / "e2e-cache"
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(cache))
    try:
        receipt, _, cached = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=True,
        )

        assert isinstance(receipt, Receipt)
        assert cached is False
        assert receipt.status == "pass", receipt.to_dict()
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


def test_legacy_python_child_process_stays_in_prepared_venv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "legacy-python-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / "setup.py").write_text(
        "from setuptools import setup\n"
        "setup(name='legacy-fixture', version='1.0.0')\n",
        encoding="utf-8",
    )
    (repository / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_child_python.py").write_text(
        "import subprocess\n"
        "\n"
        "\n"
        "def test_child_python_uses_prepared_venv():\n"
        "    completed = subprocess.run(\n"
        "        ['python', '-c', 'import sys; print(sys.executable)'],\n"
        "        check=True,\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "    )\n"
        "    assert completed.stdout.strip().startswith('/prepared/venv/')\n",
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
    monkeypatch.setenv(
        "GH_FRESHCLONE_CACHE",
        str(tmp_path / "legacy-python-e2e-cache"),
    )
    try:
        receipt, _, _ = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=True,
        )

        assert isinstance(receipt, Receipt)
        assert receipt.status == "pass", receipt.to_dict()
        step = receipt.plan.steps[0]
        assert step.evidence[:2] == ("setup.py", "tests/")
        assert step.command.startswith("PATH=/prepared/venv/bin:$PATH ")
        assert receipt.results[0].prepare_cache_hit is False
    finally:
        _cleanup_test_volumes()


def test_maven_dependencies_cross_the_offline_phase_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "maven-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / "pom.xml").write_text(
        """
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
                             https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>offline-boundary</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.11.4</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.13.0</version>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.5.2</version>
      </plugin>
    </plugins>
  </build>
</project>
""".lstrip(),
        encoding="utf-8",
    )
    test_source = repository / "src" / "test" / "java" / "example"
    test_source.mkdir(parents=True)
    (test_source / "BaselineTest.java").write_text(
        """
package example;

import static org.junit.jupiter.api.Assertions.assertEquals;
import org.junit.jupiter.api.Test;

class BaselineTest {
    @Test
    void dependenciesRemainAvailableOffline() {
        assertEquals(4, 2 + 2);
    }
}
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
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "maven-e2e-cache"))
    try:
        receipt, _, _ = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=True,
        )

        assert isinstance(receipt, Receipt)
        assert receipt.status == "pass", receipt.to_dict()
        assert receipt.plan.steps[0].ecosystem == "maven"
        assert receipt.results[0].test_network == "none"
        assert receipt.results[0].failed_phase is None
        assert receipt.results[0].prepare_cache_hit is False
        log = Path(receipt.results[0].log_path).read_text(encoding="utf-8")
        assert "prepare (network enabled) phase" in log
        assert "test (network none) phase" in log
        assert "Tests run: 1, Failures: 0, Errors: 0, Skipped: 0" in log
    finally:
        _cleanup_test_volumes()


def test_cmake_tools_cross_the_offline_phase_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "cmake-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 3.20...4.0)
project(FreshcloneCMakeBoundary LANGUAGES CXX)
include(CTest)
include(FetchContent)
FetchContent_Declare(
  fmt
  URL https://github.com/fmtlib/fmt/archive/2a2d9edb257322bec0f7ac602fde3b382fe0082a.tar.gz
  URL_HASH SHA256=1f3c8b65c29d772ab6185b5c6b663ba323f92031fadcbc47cfce07d4ef075434
  DOWNLOAD_EXTRACT_TIMESTAMP FALSE
)
FetchContent_MakeAvailable(fmt)
add_executable(freshclone_cmake_test test.cpp)
target_link_libraries(freshclone_cmake_test PRIVATE fmt::fmt)
add_test(NAME freshclone_cmake_test COMMAND freshclone_cmake_test)
""".lstrip(),
        encoding="utf-8",
    )
    (repository / "test.cpp").write_text(
        """
#include <fmt/format.h>

int main() {
    return fmt::format("cmake offline boundary {}", "passed") ==
                   "cmake offline boundary passed"
               ? 0
               : 1;
}
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
    monkeypatch.setenv("GH_FRESHCLONE_CACHE", str(tmp_path / "cmake-e2e-cache"))
    try:
        receipt, _, _ = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=True,
        )

        assert isinstance(receipt, Receipt)
        assert receipt.status == "pass", receipt.to_dict()
        assert receipt.plan.steps[0].ecosystem == "cmake"
        assert receipt.results[0].test_network == "none"
        assert receipt.results[0].failed_phase is None
        assert receipt.results[0].prepare_cache_hit is False
        assert receipt.results[0].prepared_volume
        log = Path(receipt.results[0].log_path).read_text(encoding="utf-8")
        assert "prepare (network enabled) phase" in log
        assert "test (network none) phase" in log
        assert "100% tests passed, 0 tests failed out of 1" in log

        repeated, _, repeated_cached = check_repository(
            str(repository),
            runner=E2E_RUNNER,
            use_cache=False,
            echo=True,
        )

        assert isinstance(repeated, Receipt)
        assert repeated.status == "pass", repeated.to_dict()
        assert repeated_cached is False
        assert repeated.results[0].prepare_cache_hit is True
        repeated_log = Path(repeated.results[0].log_path).read_text(encoding="utf-8")
        assert "Building CXX object" in repeated_log
        assert "100% tests passed, 0 tests failed out of 1" in repeated_log
    finally:
        _cleanup_test_volumes()


def test_test_network_requires_operator_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "network-policy-repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / ".gh-freshclone.toml").write_text(
        """
version = 1

[[steps]]
ecosystem = "network-policy"
image = "docker.io/library/alpine:3.22"
command = "if [ -e /sys/class/net/eth0 ]; then echo network=enabled; else echo network=none; fi"
test_network = "enabled"
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
    monkeypatch.setenv(
        "GH_FRESHCLONE_CACHE",
        str(tmp_path / "network-policy-cache"),
    )

    offline, _, _ = check_repository(
        str(repository),
        runner=E2E_RUNNER,
        use_cache=False,
        echo=True,
    )
    enabled, _, _ = check_repository(
        str(repository),
        runner=E2E_RUNNER,
        use_cache=False,
        echo=True,
        test_network="enabled",
    )

    assert isinstance(offline, Receipt)
    assert isinstance(enabled, Receipt)
    assert offline.status == "pass", offline.to_dict()
    assert enabled.status == "pass", enabled.to_dict()
    assert offline.results[0].test_network == "none"
    assert enabled.results[0].test_network == "enabled"
    assert "network=none" in offline.results[0].detail
    assert "network=enabled" in enabled.results[0].detail
    assert "kept every test step offline" in offline.plan.warnings[-1]


@pytest.mark.skipif(
    E2E_RUNNER != "container",
    reason="SIGTERM lifecycle probe is specific to Apple container",
)
def test_apple_container_removes_named_vm_on_sigterm(tmp_path: Path) -> None:
    name = f"gh-freshclone-e2e-sigterm-{os.getpid()}"
    log_path = tmp_path / "sigterm.log"
    command = [
        "container",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--tmpfs",
        "/tmp",
        "--entrypoint",
        "sh",
        "docker.io/library/node:24-bookworm",
        "-c",
        "sleep 120",
    ]
    child = (
        "from pathlib import Path\n"
        "from gh_freshclone.runners import _execute_phase\n"
        f"_execute_phase(runner='container', command={command!r}, "
        f"container_name={name!r}, log_path=Path({str(log_path)!r}), "
        "phase='SIGTERM E2E', echo=False)\n"
    )
    process = subprocess.Popen(  # nosec B603
        [sys.executable, "-c", child],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def container_exists() -> bool:
        listed = subprocess.run(  # nosec B603
            ["container", "list", "--all"],
            check=False,
            capture_output=True,
            text=True,
        )
        return name in listed.stdout

    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not container_exists():
            if process.poll() is not None:
                pytest.fail(f"phase process exited early with {process.returncode}")
            time.sleep(0.1)
        assert container_exists(), "named Apple container did not start"

        process.terminate()
        assert process.wait(timeout=20) == 128 + signal.SIGTERM

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and container_exists():
            time.sleep(0.1)
        assert not container_exists(), "SIGTERM left a named Apple container behind"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        subprocess.run(  # nosec B603
            ["container", "stop", name],
            check=False,
            capture_output=True,
        )
        subprocess.run(  # nosec B603
            ["container", "delete", name],
            check=False,
            capture_output=True,
        )


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
            echo=True,
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
            echo=True,
        )

        assert isinstance(repeated, Receipt)
        assert repeated.status == "pass", repeated.to_dict()
        assert repeated_cached is False
        assert repeated.results[0].prepare_cache_hit is True
    finally:
        _cleanup_test_volumes()
