from __future__ import annotations

from gh_freshclone.diagnostics import diagnose_failure, is_diagnostic_output


def test_missing_executable_has_structured_package_hint() -> None:
    status, diagnostics = diagnose_failure(
        1,
        "FileNotFoundError: [Errno 2] No such file or directory: 'less'",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "missing_executable"
    assert diagnostics[0].subject == "less"
    assert diagnostics[0].suggested_package == "less"
    assert diagnostics[0].confidence == "high"


def test_assertion_failure_remains_repository_failure() -> None:
    status, diagnostics = diagnose_failure(1, "AssertionError: expected 2, got 3")

    assert status == "test_failure"
    assert diagnostics == ()


def test_verified_failure_related_executable_is_environment_gap() -> None:
    status, diagnostics = diagnose_failure(
        1,
        "FAILED tests/test_pager.py::test_output[less]",
        observed_missing_executables=("less",),
    )

    assert status == "environment_gap"
    assert diagnostics[0].subject == "less"
    assert diagnostics[0].confidence == "medium"
    assert diagnostics[0].evidence == (
        "failed test selector references less",
        "content-addressed image probe could not find less",
    )


def test_deno_dns_failure_under_offline_policy_is_an_environment_gap() -> None:
    status, diagnostics = diagnose_failure(
        1,
        (
            "TypeError: fetch failed\n"
            "Caused by: dns error: failed to lookup address information"
        ),
        test_network="none",
        failed_phase="test",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "network_policy"


def test_gradle_offline_runtime_dependency_is_an_environment_gap() -> None:
    status, diagnostics = diagnose_failure(
        1,
        (
            "Could not resolve org.junit.platform:junit-platform-launcher:6.1.2.\n"
            "No cached version available for offline mode."
        ),
        test_network="none",
        failed_phase="test",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "network_policy"


def test_gradle_testkit_distribution_download_is_an_environment_gap() -> None:
    detail = (
        "Could not install Gradle distribution from "
        "'https://services.gradle.org/distributions/gradle-9.0.0-bin.zip'.\n"
        "Caused by: java.net.UnknownHostException: services.gradle.org"
    )

    assert is_diagnostic_output(detail)
    status, diagnostics = diagnose_failure(
        1,
        detail,
        test_network="none",
        failed_phase="test",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "network_policy"


def test_offline_external_content_lookup_is_an_environment_gap() -> None:
    status, diagnostics = diagnose_failure(
        1,
        (
            "We could not parse the content at "
            "https://download.example.invalid/p2.index\n"
            "The build is running offline."
        ),
        test_network="none",
        failed_phase="test",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "network_policy"


def test_external_terms_are_never_accepted_automatically() -> None:
    status, diagnostics = diagnose_failure(
        1,
        "The Gradle Terms of Use have not been agreed to.",
        test_network="enabled",
        failed_phase="prepare",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "external_agreement_required"
    assert "will not accept automatically" in diagnostics[0].message


def test_missing_gradle_java_toolchain_is_an_environment_gap() -> None:
    detail = (
        "Cannot find a Java installation on your machine matching: "
        "{languageVersion=17}. "
        "Toolchain download repositories have not been configured."
    )

    assert is_diagnostic_output(detail)
    status, diagnostics = diagnose_failure(
        1,
        detail,
        test_network="enabled",
        failed_phase="prepare",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "missing_java_toolchain"


def test_prepare_unknown_host_is_infrastructure_failure() -> None:
    status, diagnostics = diagnose_failure(
        1,
        "java.net.UnknownHostException: repo.maven.apache.org",
        test_network="enabled",
        failed_phase="prepare",
    )

    assert status == "infra_failure"
    assert diagnostics[0].kind == "dependency_preparation_infrastructure"


def test_missing_shared_library_has_structured_package_hint() -> None:
    status, diagnostics = diagnose_failure(
        1,
        (
            "chrome: error while loading shared libraries: "
            "libgobject-2.0.so.0: cannot open shared object file"
        ),
        test_network="enabled",
        failed_phase="test",
    )

    assert status == "environment_gap"
    assert diagnostics[0].kind == "missing_shared_library"
    assert diagnostics[0].subject == "libgobject-2.0.so.0"
    assert diagnostics[0].suggested_package == "libglib2.0-0"


def test_read_only_runner_metadata_has_actionable_storage_diagnostic() -> None:
    status, diagnostics = diagnose_failure(
        125,
        (
            "Error waiting for container: write "
            "/var/lib/desktop-containerd/daemon/meta.db: read-only file system"
        ),
        failed_phase="prepare",
    )

    assert status == "infra_failure"
    assert diagnostics[0].kind == "runner_storage"
    assert "Free host storage" in diagnostics[0].message
