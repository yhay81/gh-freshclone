# gh-freshclone

`gh-freshclone` is a baseline compiler for unfamiliar public GitHub
repositories and local Git checkouts.
Before a human or coding agent starts editing, it answers:

> What is the fastest trustworthy check for this exact commit, does it pass
> from a clean checkout, and what evidence produced that conclusion?

It compiles repository-owned manifests into an evidence-backed plan, executes
the plan without host credentials in an OCI container, and writes a reusable
JSON receipt. It distinguishes repository test failures from runner failures
and missing environment capabilities.

Automatic detection covers root-level Python, Node.js/Bun/Deno, Rust, Go,
Maven, Gradle, and CMake projects. A repository-owned configuration compiles
explicit monorepo steps. The package is alpha software.

## Why it exists

- Readiness graders inspect files but generally do not prove a baseline.
- Local GitHub Actions runners reproduce workflows, not the smallest useful
  contributor baseline.
- Environment generators create an environment but do not record whether the
  baseline was green.

`gh-freshclone` is deliberately narrower. It resolves an immutable commit,
selects a profile, fingerprints dependency inputs, prepares dependencies with
network access, then runs the inferred test in a separate container under its
declared network policy. It records the exact image digest, runner version,
phase timings, diagnostics, and logs.

Its differentiated use case is the **pre-edit external-repository check**:
an OSS contributor or coding agent often cannot add a task contract to the
target repository and should not mutate it just to learn whether its current
baseline is green. `gh-freshclone` produces that evidence directly from a
remote exact commit, with zero target-repository configuration for supported
stacks.

This is not a general repo-readiness score, service orchestrator, toolchain
provisioner, or organization-policy platform. Contract-first readiness tools
are a better fit when the repository owner wants to model services,
environments, and long-lived workflows. `gh-freshclone` is the smaller
credential-free proof primitive that an external agent can call before making
its first edit.

## Install

Prerequisites:

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/)
- Git
- Docker, Podman, or Apple `container` 1.0.0+ on a supported Mac

Until the package is registered on PyPI, install the signed release tag
directly from GitHub:

```shell
uv tool install "gh-freshclone @ git+https://github.com/yhay81/gh-freshclone.git@v0.11.0"
gh-freshclone doctor
```

After publication to PyPI:

```shell
uv tool install gh-freshclone
gh-freshclone doctor
```

`doctor` verifies Git and runner-daemon readiness, not only whether the
executables are present. Public GitHub resolution uses credential-free HTTPS;
no GitHub token, API quota, or `gh auth login` is required. Use
`doctor --json` in automation.

During development:

```shell
uv sync --locked
uv run gh-freshclone doctor
```

The supported interface is the standalone `gh-freshclone` executable.
GitHub CLI extension packaging is not currently claimed.

## Quick start

Inspect a plan without executing repository code:

```shell
gh-freshclone plan pallets/itsdangerous
gh-freshclone plan owner/repo --ref v1.2.3 --profile reproduce
gh-freshclone plan https://github.com/owner/repo/pull/123
gh-freshclone plan . --json
```

Automatic planning reads the immutable Git tree, then hydrates only the
committed manifests and evidence files used by detection. It does not download
the repository's source and test bodies. `check` expands that same
credential-free checkout to the complete exact commit only after it has found
an executable baseline. Repository-owned `.gh-freshclone.toml` configurations
retain a complete checkout during planning because their declared step paths
can intentionally reference arbitrary committed files.

Read the upstream GitHub CI state for the same exact commit without executing
repository code:

```shell
gh-freshclone github-status owner/repo
gh-freshclone github-status owner/repo --ref FULL_40_CHAR_SHA
gh-freshclone github-status https://github.com/owner/repo/pull/123 --json
```

Execute the default quick baseline:

```shell
gh-freshclone check pallets/itsdangerous
gh-freshclone check https://github.com/owner/repo/commit/FULL_40_CHAR_SHA
gh-freshclone check https://github.com/owner/repo/pull/123
gh-freshclone check . --runner docker --cpus 4 --memory 8g
gh-freshclone check owner/repo --test-network enabled
gh-freshclone check owner/repo --json
```

Profiles make the cost/coverage choice explicit:

| Profile | Purpose | Typical policy |
|---|---|---|
| `quick` | Fast trustworthy pre-edit signal | One compatible tox environment; primary test script |
| `reproduce` | Repository-default contributor baseline | Default tox/test command |
| `full` | Broad pre-submit confidence | All known scripts, features, or environments |

The profile, preparation and test commands, operator-selected test-network
policy, supporting manifest evidence, and dependency fingerprint are part of
the plan digest. Test containers stay offline by default even when
repository-owned configuration requests network. `--test-network enabled` is
the explicit operator opt-in that enables outbound access for every test step;
the effective policy is visible in `plan`, receipts, and results. Offline and
network-enabled PASS indexes are separate and cannot satisfy one another. A
PASS is reused only for the same commit, plan version, execution policy,
profile, runner, resources, and test-network policy. The index is checked
before a new clone, so a known PASS normally returns in well under a second
after commit resolution.

Use `--no-cache` when fresh execution evidence is required.
When automation already knows a full 40-character commit SHA, pass it with
`--ref` or paste a GitHub commit URL. Ref resolution then makes no network
request; materialization still verifies that the object exists before
execution. A pasted pull-request URL resolves its advertised head ref without
requiring GitHub API authentication.

`github-status` is the explicit GitHub REST API integration. It uses API
version `2026-03-10` to read the latest GitHub Checks and combined legacy
commit status for the resolved public commit. The command makes two
unauthenticated, read-only API requests, exposes the remaining API quota, does
not retry a rate-limit response, and never executes repository code. GitHub's
unauthenticated public-data limit is currently 60 requests per hour per source
IP. The mutable upstream CI observation is intentionally separate from plan,
receipt, and PASS-cache identity.

The ordinary `plan` and `check` paths continue to use credential-free Git
smart HTTP and do not require a GitHub API quota or token. See
[the GitHub integration note](docs/github-developer-program.md) for API
boundaries and Developer Program evidence.

## What a result means

The result taxonomy is intentionally small:

| Status | Meaning |
|---|---|
| `PASS` | Every compiled step passed |
| `TEST_FAILURE` | The repository baseline executed and failed |
| `ENVIRONMENT_GAP` | Required executable, library, or permission is absent |
| `INFRA_FAILURE` | The OCI runner or image could not execute the plan |

For implicit environment failures, the tool does not rely on a test name
alone. It extracts executable candidates from failing selectors, then probes
the same content-addressed image. A diagnostic includes its confidence,
evidence, and a package hint when known.

Example:

```text
Result:     ENVIRONMENT_GAP
  python: environment_gap
    Failing tests reference an executable absent from the image: less
    (suggested package: less)
```

CLI exit codes are stable:

| Code | Meaning |
|---|---|
| `0` | The check passed, the plan has executable steps, or diagnostics are healthy |
| `1` | A baseline did not pass, no plan step was detected, or `doctor` found a missing prerequisite |
| `2` | The request, repository, configuration, or runner could not be initialized |

After argument parsing, commands invoked with `--json` also return code-2
initialization failures as a JSON error envelope on standard output:

```json
{
  "api_version": 1,
  "status": "error",
  "error": {
    "code": "initialization_error",
    "message": "failed to pull image ..."
  }
}
```

## Reproducibility and performance

- GitHub targets are resolved to a full commit SHA using an isolated,
  credential-free `git ls-remote` request; exact SHA inputs skip this request.
- Public targets materialize with a depth-one fetch of only that resolved ref
  or commit. A ref move falls back to the already-resolved SHA and fails closed
  if the immutable target is no longer available.
- Mutable image tags are refreshed from their registry and resolved to
  `repository@sha256:...`; a stale local tag is never silently trusted. The
  digest is used for execution and stored in the receipt. An explicitly
  declared digest can run from verified local content without a registry
  round trip.
- Lockfiles and relevant manifests form a dependency fingerprint.
- Go reuses module and build caches but compiles `go test -count=1`, so a
  fresh proof cannot silently reuse a previous successful test result.
- Maven and Gradle reuse repository-, image-, and dependency-scoped
  app-managed volumes. Dependencies are prepared with network access, then the
  lifecycle is rerun from a fresh read-only checkout with Maven offline mode
  or Gradle `--offline` in a network-disabled container. Managed volumes avoid
  host UID/permission coupling without granting broader container privileges.
- CMake planning requires a literal root CTest signal and refuses a declared
  minimum newer than its pinned CMake runtime. It conservatively enables one
  ordinary project test option when the committed `option(...)` name and
  description identify it, while excluding CUDA, conformance, sanitizer,
  benchmark, and integration variants.
- CMake 3.31.10 and Ninja 1.13.0 are installed into an exact-commit managed
  volume during the network-enabled preparation phase of a common
  multi-architecture Python/buildpack image. A configure-only preparation
  populates declared FetchContent sources in that volume. A fresh
  configuration, two-worker build, and CTest then run with
  `FETCHCONTENT_FULLY_DISCONNECTED=ON` and no container network; compiled
  outputs are not reused. `ctest --no-tests=error` prevents a zero-test build
  from becoming a false PASS.
- Canonical CPU and memory limits are recorded in receipt v6 and participate
  in receipt and PASS-index identity; a proof produced at `2 CPU / 4g` is
  never reused for an `8g` request.
- Download caches are isolated by repository, runner, ecosystem, dependency
  fingerprint, and resolved image digest. Repository namespaces include a
  stable hash of the GitHub canonical name or local checkout path, so
  sanitized display-name collisions cannot share state.
- Preparation identity includes the dependency fingerprint, resolved image,
  preparation command, and working directory—not the test command. Changing a
  test selector does not redownload dependencies, while changing preparation
  still invalidates the cache.
- Python `.venv`/tox state, Node.js/Bun `node_modules`, and Maven/Gradle
  dependency state are kept in exact-commit, step-scoped volumes. A success
  marker allows a fresh proof to skip redundant dependency preparation
  without treating an interrupted preparation as reusable.
- A failed preparation discards only its
  repo/commit/lockfile/image-scoped host cache or prepared volumes before the
  app performs one clean retry in the same check. This prevents
  storage-corrupted partial package-manager state from becoming a persistent
  false repository failure without creating an unbounded retry loop. A second
  preparation failure leaves the scoped cache clean and returns normally.
  Test-phase failures retain successfully prepared dependencies for fast
  deterministic reruns.
- Deno's `DENO_DIR`, local vendor tree, and `node_modules` share the same
  exact-commit volume across the networked preparation and offline test
  containers, including repositories with `"vendor": true`.
- Bun baselines bootstrap a pinned Bun binary into the prepared volume of a
  Node LTS image. Hybrid test scripts can therefore use `bun`, `node`, and
  `npm` without a project-maintained custom image.
- When a Node/Bun package declares an entrypoint that is absent from the
  checkout and also owns a known `build` script, that build is compiled before
  `test`; committed entrypoints keep the faster test-only quick path.
- PASS receipts use a versioned pre-clone index.
- Matching repository/commit/profile/runner probes take a cross-process lock;
  concurrent agents neither corrupt a shared prepared volume nor duplicate a
  known PASS build.

On a Windows 11 / Docker 29.6.1 development machine, the
`pallets/itsdangerous` quick profile improved from a 131.7-second full tox run
to 5.1–25.2 seconds of runner time across repeated measurements. Reusing its
PASS avoids cloning and container startup after immutable-ref resolution.
The credential-free default-branch path currently returns in a 0.611-second
median over 10 sequential runs. Supplying its exact SHA reduces the median to
0.176 seconds with a 0.204-second p95 for the installed CLI because no remote
ref request is needed. A dependency-light entry point returns `--version` in
53.490 ms median and 63.692 ms p95 instead of loading the execution engine.
Below CLI startup, the exact-SHA in-process cache contract measures 2.992 ms
median and 4.414 ms p95 over 30 Windows runs. The isolated
local-repository hot-path benchmark measures 60.720 ms median and 67.272 ms
p95 because local Git metadata and commit resolution use one process. CI
fails if the same clone-free path exceeds 250 ms p95 on its Linux runner.
These are development measurements, not cross-platform guarantees.

A public-repository trial across eight earlier automatic ecosystems, including
the defects discovered during real execution and measured cold/warm results,
is recorded in
[`docs/field-trial-2026-07-25.md`](docs/field-trial-2026-07-25.md).
On the final dual-build Java target, verified dependency reuse reduced an
otherwise fresh end-to-end Maven-plus-Gradle proof from 897.90 to 318.27
seconds while rerunning both offline lifecycles.

The CMake follow-up planned ten current public repositories and accepted eight
with root test evidence. A physical proof of
`fmtlib/fmt@2a2d9edb257322bec0f7ac602fde3b382fe0082a` compiled and passed all
21 CTest cases with the test container offline. The first end-to-end check took
175.09 seconds, including 9.97 seconds of fresh preparation and 156.65 seconds
of build/test runner time. Reusing that exact PASS returned in 0.42 seconds.

A fresh probe of public pull request
[`pallets/itsdangerous#428`](https://github.com/pallets/itsdangerous/pull/428)
resolved its advertised head commit without GitHub API authentication,
compiled one Python step, prepared dependencies, and passed the test in a
second network-disabled container. An earlier execution-policy-v13 cold proof took
9.327 seconds of runner time; an intentionally fresh second proof reused the
verified preparation and took 5.077 seconds while still rerunning all 297
tests offline. This is the same pasted-PR workflow exposed by the CLI, not a
fixture-only code path.

Run the performance contract against any committed local checkout:

```shell
uv run python -m benchmarks.cached_workflow . \
  --runner docker --iterations 30 --max-p95-ms 250
uv run python -m benchmarks.cli_startup --max-p95-ms 250
```

The Python runner uses an exact-version
[`uv` derived image](https://docs.astral.sh/uv/guides/integration/docker/), so
it avoids installing the package manager on every run.

## Cache operations

Cache locations:

- macOS: `~/Library/Caches/gh-freshclone`
- Linux: `${XDG_CACHE_HOME:-~/.cache}/gh-freshclone`
- Windows: `%LOCALAPPDATA%\gh-freshclone`

Set `GH_FRESHCLONE_CACHE` to override the host location.

```shell
gh-freshclone cache status
gh-freshclone cache status --json
gh-freshclone cache prune
gh-freshclone cache prune --max-gib 3 --max-volume-gib 2 \
  --max-volumes 12 --max-age-days 14
gh-freshclone cache prune --max-evidence-gib 0.5 --max-evidence-entries 256
```

Automatic maintenance runs daily, and also after a preparation cache miss when
measured prepared-volume usage has crossed its hard limit. Cache hits do not
pay for this extra measurement. Defaults are 5 GiB and 128 dependency-cache
entries, 1 GiB and 512 receipt/log evidence bundles, 24 prepared volumes with a
3 GiB measured-volume budget, and 30 days. A cache hit refreshes its receipt's
LRU time. Docker and Podman volume usage comes from runner metadata without
mounting or executing cached content; Apple `container` retains the count and
age limits when byte accounting is unavailable. Runner readiness, version,
image/volume metadata, volume creation, failure diagnostics, cache metadata,
and cleanup calls are limited to 15 seconds, so a stopped runner cannot turn
startup, `doctor`, `cache status`, or failure handling into an unbounded wait.
Independent version/readiness probes run concurrently while preserving runner
preference order, so the worst-case wait does not multiply by the number of
installed runners. Image downloads and repository prepare/test phases keep
their normal completion semantics.
Non-zero runs explicitly retry named container cleanup, and Docker/Podman
execution containers carry app ownership labels for diagnosis.

Before a cache-miss execution, the app preserves 2 GiB free on the filesystems
holding its cache and temporary checkout. It first removes only reclaimable
app-owned cache, tightening the measured prepared-volume budget further when
the normal limits are not enough; if the reserve is still unavailable, the
check stops before cloning with an actionable error. An existing immutable
PASS receipt remains usable because it needs neither a checkout nor a
container.
Evidence from incompatible receipt/execution-policy versions, dangling PASS
indexes, and interrupted-run logs are recovered by the same maintenance path.
It never invokes a global runner prune. Removal is limited to host paths below
the app cache root and named volumes recorded by this app with the strict
`ghfc-*` format. Per-resource OS locks protect every in-use dependency cache,
receipt/log bundle, and prepared volume from automatic or manually invoked
pruning, including from another process. Unrelated repositories remain
parallel. Each step log is capped at 10 MiB while runner output continues to
be drained; a truncation marker and diagnostic tail remain available.

## macOS, Windows, and Linux

Plan compilation works natively on all three platforms:

| Host | `--runner auto` preference |
|---|---|
| macOS | Apple `container`, Docker, Podman |
| Windows | Docker, Podman |
| Linux | Docker, Podman |

`auto` skips a stopped Docker/Podman daemon when another installed runner is
ready. If all daemons are stopped, an existing pre-clone PASS remains
available; a cache miss stops before cloning with an actionable readiness
error.

[Apple `container`](https://github.com/apple/container) runs Linux containers
in lightweight virtual machines. Apple silicon with macOS 26 is the supported
configuration. Version 1.0.0 or newer is required because the runner contract
uses stable capability, read-only mount, named-volume, no-network, and
resource-limit flags. Apple `container` accepts whole-number CPU limits;
Docker and Podman also accept fractional values. Intel Macs and older macOS
releases can use Docker or Podman.

```shell
gh-freshclone check owner/repo --runner container
gh-freshclone check owner/repo --runner docker
```

Command construction, read-only input, prepared volumes, and `--network none`
are covered by the cross-platform contract suite. A manually dispatched
`apple-container-e2e.yml` workflow runs the complete prepare-then-offline-test
probe on a dedicated self-hosted runner labelled
`gh-freshclone-apple-container`. GitHub-hosted macOS runners cannot provide
this proof because [nested virtualization is not
supported](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#limitations).
A physical Apple silicon run and cold/warm public-repository measurements are
recorded in
[`docs/field-trial-2026-07-25.md`](docs/field-trial-2026-07-25.md#physical-apple-container-validation).

## Security model

Repository tests execute untrusted code. `gh-freshclone` therefore:

- creates a fresh checkout pinned to an exact commit;
- excludes uncommitted and untracked local files;
- resolves public GitHub refs with a fixed HTTPS URL, isolated empty Git
  configuration, disabled prompts, and no API token;
- uses credential-free HTTPS for public GitHub clones and refuses known
  private repositories;
- disables system/global Git config, checkout templates, interactive
  credential helpers, system attributes, and host-configured filters during
  materialization;
- never forwards `GH_TOKEN`, `GITHUB_TOKEN`, SSH agents, Git configuration, or
  the broad host environment;
- gives the test a disposable workspace copied from `/input` inside the
  container;
- mounts `/input` read-only with every supported runner;
- mounts only repository-scoped dependency caches;
- runs dependency preparation with network access, exits that container, and
  starts a distinct test container with network disabled by default;
- never lets repository-owned configuration enable outbound test access by
  itself; only the caller's `--test-network enabled` opt-in can do so;
- drops capabilities, restoring only `CHOWN` and `FOWNER` for package
  extraction;
- enables `no-new-privileges` with Docker and Podman;
- limits CPU and memory on every runner, and process count on Docker/Podman;
- mounts `/tmp` as a disposable tmpfs on every runner;
- removes named test containers on normal completion, error, or interruption.

Only dependency preparation normally needs outbound network access. For a
suite that intentionally downloads assets during its test phase, inspect the
plan and pass `--test-network enabled`. A repository may request the same
policy in `.gh-freshclone.toml`, but that request remains offline until the
operator explicitly opts in. An OCI runtime remains a security boundary with
a non-zero attack surface. Do not add credential or broad host mounts for
untrusted repositories.

## Detection policy

Commands come only from known manifest structures. README snippets and
arbitrary GitHub Actions `run` blocks are not executed.

Current evidence sources include:

- Python: `pyproject.toml`, `uv.lock`, dependency groups/extras, tox and pytest;
  legacy `setup.py`/`setup.cfg` projects and common requirements-file names
  are also compiled without executing `setup.py` on the host
- Node.js: `packageManager`, lockfiles, and known package scripts
- Rust: `Cargo.toml` and `rust-toolchain.toml`
- Go: `go.mod`, using its explicit `toolchain` when present and a refreshed
  stable image otherwise
- Maven: `pom.xml`, an optional committed `mvnw`, and wrapper configuration;
  `test` is the default lifecycle and `full` compiles `verify`
- Gradle: committed build files plus a committed `gradlew`; wrapper
  compatibility and literal Java 17/21/25 toolchain declarations select a
  multi-architecture official Gradle image, while unwrapped builds are never
  auto-run

Cargo members declared by a root workspace are covered by its
`cargo test --workspace` step. Other nested manifests are reported as not
auto-run rather than guessed. A monorepo can opt into explicit, reviewable compilation with
`.gh-freshclone.toml`:

```toml
version = 1

[[steps]]
profiles = ["quick", "reproduce", "full"]
path = "services/api"
ecosystem = "python"
image = "ghcr.io/astral-sh/uv:0.11.32-python3.13-trixie"
prepare_command = "uv sync --frozen --group test"
command = "uv run --offline --no-sync --group test pytest -q"
test_network = "none"
dependency_files = ["uv.lock"]

[[steps]]
profiles = ["full"]
path = "apps/web"
ecosystem = "node"
image = "docker.io/library/node:24-bookworm"
prepare_command = "npm ci --no-audit --no-fund"
command = "npm test"
test_network = "none"
dependency_files = ["package-lock.json"]
```

The configuration is authoritative when present. Paths and dependency files
must remain below the repository, unknown fields fail closed, and `plan` still
does not execute commands. `test_network = "enabled"` is only a repository
request: the effective policy remains `none` unless the caller passes
`--test-network enabled`. This prevents an unfamiliar repository from granting
itself outbound access. The compiled plan records `working_directory`
separately, and the runner mounts each prepared environment at that exact
subdirectory rather than relying on a shell-only `cd`.

## Agent and Tsumugi integration

The library API avoids scraping human CLI output:

```python
from pathlib import Path

from gh_freshclone.api import (
    compile_materialized_checkout,
    probe_repository,
    receipt_schema,
)
from gh_freshclone.integrations.tsumugi import outcome_to_tsumugi_flags
from gh_freshclone.model import Repository

outcome = probe_repository("pallets/itsdangerous", profile="quick", echo=False)
assert outcome.api_version == 1
print(
    outcome.status,
    outcome.receipt_path,
    outcome.cached,
    outcome.elapsed_seconds,
)
tsumugi_flags = outcome_to_tsumugi_flags(outcome)

# A host system can retain its own clone and sandbox lifecycle.
repository = Repository(
    display_name="owner/repo",
    commit_sha="0" * 40,
    ref="main",
    source_url=None,
    github_repository="owner/repo",
    local_path=None,
)
plan = compile_materialized_checkout(Path("/exact/checkout"), repository)
schema = receipt_schema()
```

`compile_materialized_checkout` only compiles manifests; it does not execute
repository code. The caller guarantees that the checkout matches the supplied
commit. This is the integration seam for Tsumugi: share the compiler and
evidence contract while Tsumugi retains its systemd sandbox, stall detection,
rate controls, and state database. `outcome_to_tsumugi_flags` maps status,
commands, commit, diagnostics, image identities, and cache evidence to
Tsumugi's existing `test_cmd` / `baseline_*` flag contract; it does not bypass
Tsumugi's quality gate or external-action controls.

`check --json` adds `api_version`, `receipt_path`, `cached`, and end-to-end
`elapsed_seconds` to the receipt shape. Receipt v6 is documented by the bundled
`gh_freshclone/schemas/receipt-v6.schema.json`. It records canonical CPU and
memory limits, each step's working directory, and whether dependency
preparation was reused; resource limits also scope cache reuse. The Tsumugi
adapter preserves nested working directories and exposes limits as
`baseline_resource_limits`.

## Development

```shell
uv sync --locked
uv run pytest -q
uv run ruff check .
uv build
uv run python -m benchmarks.cached_workflow . --runner docker
uv run python -m benchmarks.cli_startup
```

CI runs unit and local-checkout integration tests on Windows, macOS, and Linux.
It also runs a real Docker prepare/offline-test E2E and the cached-path
performance contract. Native Apple `container` E2E is available through the
dedicated self-hosted workflow.

## Release process

The tag-triggered `release.yml` workflow builds and smoke-tests the wheel and
sdist, records SHA-256 checksums and GitHub artifact attestations, and publishes
an independent GitHub release. PyPI publication runs only when the repository
variable `PYPI_TRUSTED_PUBLISHING` is exactly `true` and the `pypi` environment
is connected as a Trusted Publisher; it requires no long-lived publishing
token. A `vX.Y.Z` tag must exactly match the project version.

Before publishing, the workflow reruns tests, lint, the native Docker E2E, and
the performance contract. It builds with `uv build --no-sources`, smoke-tests
both the wheel and source distribution in isolated environments, writes
SHA-256 checksums, and transfers only that three-file release set to a separate
publish job. The source-running build job has read-only repository access and
no OIDC token; only the `pypi` environment job can attest, create a draft
GitHub release, and publish through PyPI Trusted Publishing. It rechecks the
transferred checksums and exposes the GitHub release only after PyPI succeeds.
Release artifacts can then be checked with:

```shell
gh attestation verify gh_freshclone-X.Y.Z-py3-none-any.whl \
  --repo yhay81/gh-freshclone
```

See [CHANGELOG.md](CHANGELOG.md) for release contents.

## Support

Use [GitHub Issues](https://github.com/yhay81/gh-freshclone/issues) for bug
reports and support. Do not include access tokens, private repository content,
or full logs from private projects. Follow the
[security policy](https://github.com/yhay81/gh-freshclone/blob/main/SECURITY.md)
for vulnerability reports. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development gates and non-negotiable execution boundaries.
The support contact is
[yusuke8h@gmail.com](mailto:yusuke8h@gmail.com).

## License

MIT
