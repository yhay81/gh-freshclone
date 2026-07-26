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

## GitHub Actions integration

Release v0.19 adds a public composite action for Linux Docker or Podman jobs.
A caller can run the same exact-commit baseline as one workflow step and
retain the complete JSON proof through the action's `result-path` output. A
public remote target needs no checkout, GitHub token, secret, API permission,
hosted service, or webhook.

The action installs `gh-freshclone` from its own immutable action ref. Caller
inputs enter a fixed Python entrypoint through environment variables, then
remain separate arguments in a non-shell `uvx` invocation. The target follows
an explicit option delimiter, and Git/GitHub credential variables are removed
from the child environment. CI exercises the local action against its exact
checked-out commit in a real Docker baseline and verifies the receipt,
execution-policy, plan, and commit identities.

## Production consumer evidence

KAGARI, the author's resident security-research coordinator, uses
`gh-freshclone` as its public-repository G1 baseline gate. The integration pins
release v0.18.0 by source commit
`800d76bea5efd80f19ac019fd6d336daaeb3ad42` and accepts only plan v10,
receipt v7, execution policy v22, and GitHub-status interface v1. It rejects
private targets, mismatched commits, malformed results, and incoherent source
provenance instead of treating them as a green baseline.

The first physical v0.18 integration proof checked
`dependabot/dependabot-core@6c8bb8bd9cb7ec79c324bc550a992ab66201e76a`
at the bounded `npm_and_yarn/helpers` component. The default offline run
preserved a high-confidence network-policy gap. One explicitly
network-enabled retry then created a fresh isolated checkout from the exact
source cache, passed `git fsck --full --strict`, reran all five suites and 25
tests, and produced a PASS proof. KAGARI recorded
`source_cache_hit=true` and `source_validation=git-fsck-full-strict`; it did
not reuse a test result.

The resident KAGARI worker runs independently of this repository's CI and
consumes the public release through the standalone CLI. This supplies a real
operator integration and repeatable runtime feedback without a hosted
`gh-freshclone` service, stored GitHub credentials, or per-user server state.

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
