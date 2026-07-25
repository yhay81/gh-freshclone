# GitHub integration and Developer Program evidence

`gh-freshclone` is a public developer tool that compiles and proves a
credential-free baseline for an exact public GitHub commit. Its
`github-status` command also integrates with the GitHub REST API so a
contributor or coding agent can compare the local proof with the upstream CI
state for the same immutable commit.

## Production/development integration

The command uses GitHub REST API version `2026-03-10` and these public,
read-only endpoints:

- `GET /repos/{owner}/{repo}/commits/{sha}/status`
- `GET /repos/{owner}/{repo}/commits/{sha}/check-runs`

It resolves the target to a full commit SHA first, requests at most 100 latest
check runs, and reports:

- canonical repository and commit identity;
- a bounded list and count of GitHub Checks;
- the combined legacy commit status and its contexts;
- whether the check-run list was truncated;
- the visible REST API quota and reset time.

The command does not execute repository code. API observations are mutable, so
they do not alter the deterministic baseline plan, receipt, or PASS-cache
identity.

## Permissions and data boundary

- Only public `OWNER/REPO` targets and GitHub repository, commit, or pull
  request URLs are accepted.
- API requests are unauthenticated and read-only. No OAuth scope, GitHub App
  installation, personal access token, webhook receiver, server, or database
  is required.
- A fixed `https://api.github.com` origin, GitHub media type, explicit API
  version, bounded response size, and 15-second deadline are enforced.
- Rate-limit responses are not retried. The reset time is returned without
  including GitHub's response body.
- Repository commands still run only inside the existing credential-free OCI
  boundary. API data and credentials are never forwarded to a test container.

The baseline-only `plan` and `check` paths remain usable without GitHub REST
API quota. The explicit status command uses two of GitHub's unauthenticated
public-data requests per invocation.

## Reproducible evidence

```shell
gh-freshclone github-status yhay81/gh-freshclone \
  --ref FULL_40_CHARACTER_COMMIT
```

The first physical integration run on 2026-07-26 read 21 successful GitHub
Checks for the v0.6.1 commit. GitHub's combined legacy endpoint returned its
API-level `pending` value with zero contexts; the application preserved that
raw value while correctly reporting the effective legacy state as `none` and
the combined observed state as `success`.

Unit tests cover fixed API origin and headers, exact endpoint construction,
response normalization, empty legacy status, more-than-100 check truncation,
private/local target rejection, bounded response size, HTTP errors, and
rate-limit handling. The command is included in the cross-platform package and
distribution smoke gates.

## Program contact

- Repository: <https://github.com/yhay81/gh-freshclone>
- Support email: <yusuke8h@gmail.com>
- Bug and support queue: <https://github.com/yhay81/gh-freshclone/issues>
- Security reporting: <https://github.com/yhay81/gh-freshclone/security>

GitHub documents Developer Program membership as open to developers and
companies with an integration in production or development using the GitHub
API and a support email:
<https://docs.github.com/en/integrations/concepts/github-developer-program>.
