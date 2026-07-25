from __future__ import annotations

import json
from datetime import UTC, datetime
from http.client import HTTPException, HTTPSConnection
from typing import Any
from urllib.parse import quote, urlsplit

from . import __version__
from .github import GitHubTarget, parse_github_target, resolve_repository

GITHUB_API_VERSION = "2026-03-10"
GITHUB_STATUS_VERSION = 1
_API_HOST = "api.github.com"
_MAX_RESPONSE_BYTES = 5 * 1024**2
_REQUEST_TIMEOUT_SECONDS = 15
_MAX_REDIRECTS = 2
_FAILURE_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "startup_failure",
    "stale",
    "timed_out",
}
_SUCCESS_CONCLUSIONS = {"neutral", "skipped", "success"}


class GitHubStatusError(RuntimeError):
    pass


def _rate_limit(headers: Any) -> dict[str, Any] | None:
    values: dict[str, int] = {}
    for key in ("limit", "remaining", "used", "reset"):
        raw = headers.get(f"X-RateLimit-{key}")
        try:
            values[key] = int(raw)
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    reset = values.pop("reset", None)
    payload: dict[str, Any] = values
    if reset is not None:
        payload["reset_at"] = datetime.fromtimestamp(reset, UTC).isoformat()
    return payload


def _http_error(status: int, headers: Any) -> GitHubStatusError:
    rate = _rate_limit(headers)
    if status in {403, 429} and rate and rate.get("remaining") == 0:
        reset_at = rate.get("reset_at", "the advertised reset time")
        return GitHubStatusError(
            "GitHub REST API rate limit exhausted; retry after "
            f"{reset_at}"
        )
    if status == 404:
        return GitHubStatusError(
            "GitHub REST API could not read this public repository or commit"
        )
    if status in {403, 429}:
        retry_after = headers.get("Retry-After")
        suffix = f"; retry after {retry_after} seconds" if retry_after else ""
        return GitHubStatusError(
            f"GitHub REST API temporarily refused the request{suffix}"
        )
    return GitHubStatusError(
        f"GitHub REST API request failed with HTTP {status}"
    )


def _redirect_path(location: str) -> str:
    parsed = urlsplit(location)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GitHubStatusError(
            "GitHub REST API redirected outside its fixed HTTPS origin"
        ) from exc
    if (parsed.scheme or parsed.netloc) and (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() != _API_HOST
            or parsed.username
            or parsed.password
            or port not in {None, 443}
    ):
        raise GitHubStatusError(
            "GitHub REST API redirected outside its fixed HTTPS origin"
        )
    if not parsed.path.startswith("/"):
        raise GitHubStatusError("GitHub REST API returned an invalid redirect")
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


def _get_json(path: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"gh-freshclone/{__version__}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    current_path = path
    for redirects in range(_MAX_REDIRECTS + 1):
        connection = HTTPSConnection(
            _API_HOST,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        try:
            connection.request("GET", current_path, headers=headers)
            response = connection.getresponse()
            response_headers = response.headers
            if response.status in {301, 302, 307, 308}:
                location = response_headers.get("Location")
                if not isinstance(location, str) or redirects == _MAX_REDIRECTS:
                    raise GitHubStatusError(
                        "GitHub REST API returned too many redirects"
                    )
                current_path = _redirect_path(location)
                continue
            if response.status != 200:
                raise _http_error(response.status, response_headers)
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise GitHubStatusError(
                    "GitHub REST API response exceeded 5 MiB"
                )
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubStatusError(
                    "GitHub REST API returned invalid JSON"
                ) from exc
            if not isinstance(payload, dict):
                raise GitHubStatusError(
                    "GitHub REST API returned an unexpected response shape"
                )
            return payload, _rate_limit(response_headers)
        except GitHubStatusError:
            raise
        except (HTTPException, OSError, TimeoutError) as exc:
            raise GitHubStatusError(
                "GitHub REST API request could not be completed"
            ) from exc
        finally:
            connection.close()
    raise GitHubStatusError("GitHub REST API returned too many redirects")


def _require_public_target(target: str) -> GitHubTarget:
    parsed = parse_github_target(target)
    if parsed is None:
        raise GitHubStatusError(
            "github-status requires a public OWNER/REPO or GitHub URL"
        )
    return parsed


def _repository_payload(value: dict[str, Any]) -> dict[str, Any]:
    full_name = value.get("full_name")
    default_branch = value.get("default_branch")
    private = value.get("private")
    if (
        not isinstance(full_name, str)
        or not isinstance(private, bool)
        or default_branch is not None
        and not isinstance(default_branch, str)
    ):
        raise GitHubStatusError(
            "GitHub REST API returned incomplete repository metadata"
        )
    if private:
        raise GitHubStatusError("private GitHub repositories are not supported")
    return {
        "full_name": full_name,
        "html_url": value.get("html_url"),
        "default_branch": default_branch,
        "archived": (
            value["archived"] if isinstance(value.get("archived"), bool) else None
        ),
        "disabled": (
            value["disabled"] if isinstance(value.get("disabled"), bool) else None
        ),
        "fork": value["fork"] if isinstance(value.get("fork"), bool) else None,
        "visibility": value.get("visibility"),
    }


def _check_state(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "none"
    conclusions = {item["conclusion"] for item in runs}
    if conclusions & _FAILURE_CONCLUSIONS:
        return "failure"
    if any(item["status"] != "completed" for item in runs):
        return "pending"
    if conclusions <= _SUCCESS_CONCLUSIONS:
        return "success"
    return "unknown"


def _checks_payload(value: dict[str, Any]) -> dict[str, Any]:
    raw_runs = value.get("check_runs")
    total_count = value.get("total_count")
    if not isinstance(raw_runs, list) or not isinstance(total_count, int):
        raise GitHubStatusError(
            "GitHub REST API returned incomplete check-run metadata"
        )
    runs: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for raw in raw_runs:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        status = raw.get("status")
        conclusion = raw.get("conclusion")
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        normalized_conclusion = (
            conclusion if isinstance(conclusion, str) else None
        )
        app = raw.get("app")
        app_slug = app.get("slug") if isinstance(app, dict) else None
        item = {
            "name": name,
            "status": status,
            "conclusion": normalized_conclusion,
            "app": app_slug if isinstance(app_slug, str) else None,
            "details_url": raw.get("details_url"),
            "started_at": raw.get("started_at"),
            "completed_at": raw.get("completed_at"),
        }
        runs.append(item)
        key = (
            normalized_conclusion or "completed"
            if status == "completed"
            else status
        )
        counts[key] = counts.get(key, 0) + 1
    truncated = total_count > len(runs)
    state = _check_state(runs)
    if truncated and state != "failure":
        state = "partial"
    return {
        "state": state,
        "total_count": total_count,
        "returned_count": len(runs),
        "truncated": truncated,
        "counts": dict(sorted(counts.items())),
        "runs": runs,
    }


def _legacy_status_payload(value: dict[str, Any]) -> dict[str, Any]:
    state = value.get("state")
    total_count = value.get("total_count")
    raw_statuses = value.get("statuses")
    if (
        not isinstance(state, str)
        or not isinstance(total_count, int)
        or not isinstance(raw_statuses, list)
    ):
        raise GitHubStatusError(
            "GitHub REST API returned incomplete commit-status metadata"
        )
    contexts: list[dict[str, Any]] = []
    for raw in raw_statuses:
        if not isinstance(raw, dict):
            continue
        context = raw.get("context")
        status_state = raw.get("state")
        if not isinstance(context, str) or not isinstance(status_state, str):
            continue
        contexts.append(
            {
                "context": context,
                "state": status_state,
                "description": raw.get("description"),
                "target_url": raw.get("target_url"),
                "updated_at": raw.get("updated_at"),
            }
        )
    return {
        "state": "none" if total_count == 0 else state,
        "api_state": state,
        "total_count": total_count,
        "returned_count": len(contexts),
        "truncated": total_count > len(contexts),
        "contexts": contexts,
    }


def _combined_state(
    checks: dict[str, Any],
    legacy_status: dict[str, Any],
) -> str:
    states = {checks["state"], legacy_status["state"]}
    if states & {"error", "failure"}:
        return "failure"
    if "pending" in states:
        return "pending"
    observed = states - {"none"}
    if observed and observed <= {"success"}:
        return "success"
    return "unknown"


def github_status(
    target: str,
    ref: str | None = None,
) -> dict[str, Any]:
    """Read public GitHub CI context for the exact commit without running code."""

    parsed = _require_public_target(target)
    if ref is not None and parsed.ref is not None:
        raise GitHubStatusError(
            "--ref cannot be combined with a GitHub commit or pull-request URL"
        )
    repository = resolve_repository(target, ref)
    if not repository.github_repository:
        raise GitHubStatusError("target has no public GitHub repository")
    owner, name = repository.github_repository.split("/", 1)
    encoded_owner = quote(owner, safe="")
    encoded_name = quote(name, safe="")
    encoded_sha = quote(repository.commit_sha, safe="")
    prefix = f"/repos/{encoded_owner}/{encoded_name}"

    raw_status, rate = _get_json(
        f"{prefix}/commits/{encoded_sha}/status?per_page=100"
    )
    raw_repository = raw_status.get("repository")
    if not isinstance(raw_repository, dict):
        raise GitHubStatusError(
            "GitHub REST API returned incomplete repository metadata"
        )
    repository_payload = _repository_payload(raw_repository)
    raw_checks, checks_rate = _get_json(
        f"{prefix}/commits/{encoded_sha}/check-runs"
        "?filter=latest&per_page=100"
    )
    checks = _checks_payload(raw_checks)
    legacy_status = _legacy_status_payload(raw_status)
    return {
        "github_status_version": GITHUB_STATUS_VERSION,
        "github_api_version": GITHUB_API_VERSION,
        "repository": repository_payload,
        "commit_sha": repository.commit_sha,
        "ref": repository.ref,
        "state": _combined_state(checks, legacy_status),
        "checks": checks,
        "legacy_status": legacy_status,
        "rate_limit": checks_rate or rate,
    }
