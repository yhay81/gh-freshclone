from __future__ import annotations

from gh_freshclone.diagnostics import diagnose_failure


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
