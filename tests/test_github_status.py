from __future__ import annotations

import json
from email.message import Message

import pytest

from gh_freshclone import github_status
from gh_freshclone.github import Repository


class _Response:
    def __init__(
        self,
        payload: dict[str, object],
        *,
        remaining: int,
        status: int = 200,
        location: str | None = None,
        body: bytes | None = None,
    ) -> None:
        self.status = status
        self._body = json.dumps(payload).encode() if body is None else body
        self.headers = Message()
        self.headers["X-RateLimit-Limit"] = "60"
        self.headers["X-RateLimit-Remaining"] = str(remaining)
        self.headers["X-RateLimit-Used"] = str(60 - remaining)
        self.headers["X-RateLimit-Reset"] = "1"
        if location is not None:
            self.headers["Location"] = location

    def read(self, limit: int) -> bytes:
        assert limit == 5 * 1024**2 + 1
        return self._body


def _install_connections(
    monkeypatch,
    responses: list[_Response],
) -> list[tuple[str, int, str, str, dict[str, str]]]:
    requests: list[tuple[str, int, str, str, dict[str, str]]] = []
    remaining = iter(responses)

    class Connection:
        def __init__(self, host: str, *, timeout: int) -> None:
            self.host = host
            self.timeout = timeout
            self.method = ""
            self.path = ""
            self.headers: dict[str, str] = {}

        def request(
            self,
            method: str,
            path: str,
            *,
            headers: dict[str, str],
        ) -> None:
            self.method = method
            self.path = path
            self.headers = headers
            requests.append(
                (self.host, self.timeout, method, path, dict(headers))
            )

        def getresponse(self) -> _Response:
            return next(remaining)

        def close(self) -> None:
            return None

    monkeypatch.setattr(github_status, "HTTPSConnection", Connection)
    return requests


def _repository() -> Repository:
    return Repository(
        display_name="owner/repo",
        commit_sha="a" * 40,
        ref="main",
        source_url="https://github.com/owner/repo",
        github_repository="owner/repo",
        local_path=None,
        is_private=False,
    )


def test_github_status_reads_exact_public_commit_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_status, "resolve_repository", lambda *args: _repository())
    responses = [
        _Response(
            {
                "state": "success",
                "total_count": 1,
                "statuses": [
                    {
                        "context": "external/ci",
                        "state": "success",
                        "description": "green",
                        "target_url": "https://ci.example.test/1",
                        "updated_at": "2026-01-01T00:01:00Z",
                    },
                ],
                "repository": {
                    "full_name": "owner/repo",
                    "html_url": "https://github.com/owner/repo",
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                    "disabled": False,
                    "fork": True,
                    "visibility": "public",
                },
            },
            remaining=59,
        ),
        _Response(
            {
                "total_count": 3,
                "check_runs": [
                    {
                        "name": "test",
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"slug": "github-actions"},
                        "details_url": "https://github.com/owner/repo/runs/1",
                        "started_at": "2026-01-01T00:00:00Z",
                        "completed_at": "2026-01-01T00:01:00Z",
                    },
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "app": {"slug": "github-actions"},
                        "details_url": "https://github.com/owner/repo/runs/2",
                    },
                    {
                        "name": "deploy",
                        "status": "in_progress",
                        "conclusion": None,
                        "app": None,
                        "details_url": None,
                    },
                ],
            },
            remaining=58,
        ),
    ]
    requests = _install_connections(monkeypatch, responses)

    payload = github_status.github_status("owner/repo")

    assert payload["github_status_version"] == 1
    assert payload["github_api_version"] == "2026-03-10"
    assert payload["repository"]["fork"] is True
    assert payload["commit_sha"] == "a" * 40
    assert payload["state"] == "failure"
    assert payload["checks"]["counts"] == {
        "failure": 1,
        "in_progress": 1,
        "success": 1,
    }
    assert payload["checks"]["state"] == "failure"
    assert payload["legacy_status"]["state"] == "success"
    assert payload["legacy_status"]["api_state"] == "success"
    assert payload["rate_limit"] == {
        "limit": 60,
        "remaining": 58,
        "used": 2,
        "reset_at": "1970-01-01T00:00:01+00:00",
    }
    assert [
        (host, timeout, method, path)
        for host, timeout, method, path, _ in requests
    ] == [
            (
                "api.github.com",
                15,
                "GET",
                f"/repos/owner/repo/commits/{'a' * 40}/status?per_page=100",
            ),
            (
                "api.github.com",
                15,
                "GET",
                (
                    f"/repos/owner/repo/commits/{'a' * 40}/check-runs"
                    "?filter=latest&per_page=100"
                ),
            ),
        ]
    for _, _, _, _, headers in requests:
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["User-Agent"].startswith("gh-freshclone/")
        assert headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert "Authorization" not in headers


def test_github_status_rejects_local_paths_before_resolution(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        github_status,
        "resolve_repository",
        lambda *args: pytest.fail("a local path must not be resolved"),
    )

    with pytest.raises(
        github_status.GitHubStatusError,
        match="requires a public OWNER/REPO",
    ):
        github_status.github_status(str(tmp_path))


def test_github_status_reports_public_not_found_without_response_body(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_status, "resolve_repository", lambda *args: _repository())
    _install_connections(
        monkeypatch,
        [
            _Response(
                {},
                remaining=59,
                status=404,
                body=b"secret-response-body",
            )
        ],
    )

    with pytest.raises(
        github_status.GitHubStatusError,
        match="could not read this public repository or commit",
    ) as captured:
        github_status.github_status("owner/repo")

    assert "secret-response-body" not in str(captured.value)


def test_github_status_reports_rate_limit_reset_without_retry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_status, "resolve_repository", lambda *args: _repository())
    requests = _install_connections(
        monkeypatch,
        [_Response({}, remaining=0, status=403)],
    )

    with pytest.raises(
        github_status.GitHubStatusError,
        match="1970-01-01T00:00:01\\+00:00",
    ):
        github_status.github_status("owner/repo")

    assert len(requests) == 1


def test_empty_legacy_status_does_not_make_green_checks_pending(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_status, "resolve_repository", lambda *args: _repository())
    responses = [
        _Response(
            {
                "state": "pending",
                "total_count": 0,
                "statuses": [],
                "repository": {
                    "full_name": "owner/repo",
                    "default_branch": "main",
                    "private": False,
                },
            },
            remaining=59,
        ),
        _Response(
            {
                "total_count": 1,
                "check_runs": [
                    {
                        "name": "test",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            },
            remaining=58,
        ),
    ]
    _install_connections(monkeypatch, responses)

    payload = github_status.github_status("owner/repo")

    assert payload["legacy_status"]["state"] == "none"
    assert payload["legacy_status"]["api_state"] == "pending"
    assert payload["state"] == "success"


def test_get_json_rejects_cross_origin_redirect(monkeypatch) -> None:
    requests = _install_connections(
        monkeypatch,
        [
            _Response(
                {},
                remaining=59,
                status=302,
                location="https://evil.example.test/steal",
            )
        ],
    )

    with pytest.raises(
        github_status.GitHubStatusError,
        match="outside its fixed HTTPS origin",
    ):
        github_status._get_json("/repos/owner/repo")

    assert len(requests) == 1


def test_get_json_allows_same_origin_https_redirect(monkeypatch) -> None:
    requests = _install_connections(
        monkeypatch,
        [
            _Response(
                {},
                remaining=59,
                status=301,
                location="https://api.github.com/repositories/1?answer=42",
            ),
            _Response({"ok": True}, remaining=58),
        ],
    )

    payload, _ = github_status._get_json("/repos/owner/repo")

    assert payload == {"ok": True}
    assert [request[3] for request in requests] == [
        "/repos/owner/repo",
        "/repositories/1?answer=42",
    ]


def test_get_json_rejects_response_larger_than_five_mib(monkeypatch) -> None:
    _install_connections(
        monkeypatch,
        [
            _Response(
                {},
                remaining=59,
                body=b"x" * (5 * 1024**2 + 1),
            )
        ],
    )

    with pytest.raises(
        github_status.GitHubStatusError,
        match="response exceeded 5 MiB",
    ):
        github_status._get_json("/repos/owner/repo")


def test_truncated_check_runs_never_claim_complete_success() -> None:
    payload = github_status._checks_payload(
        {
            "total_count": 101,
            "check_runs": [
                {
                    "name": "visible",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        }
    )

    assert payload["truncated"] is True
    assert payload["state"] == "partial"


def test_github_status_rejects_private_repository_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_status, "resolve_repository", lambda *args: _repository())
    monkeypatch.setattr(
        github_status,
        "_get_json",
        lambda path: (
            {
                "state": "pending",
                "total_count": 0,
                "statuses": [],
                "repository": {
                    "full_name": "owner/repo",
                    "default_branch": "main",
                    "private": True,
                },
            },
            None,
        ),
    )

    with pytest.raises(
        github_status.GitHubStatusError,
        match="private GitHub repositories are not supported",
    ):
        github_status.github_status("owner/repo")
