# Changelog

All notable changes to `gh-freshclone` are documented here. The project uses
semantic versioning while its public receipt, execution-policy, plan, and
adapter versions evolve independently.

## Unreleased

## 0.4.0

- Materialize public GitHub targets with a credential-free, depth-one fetch of
  only the resolved ref or exact commit instead of cloning default-branch
  history. If a ref moves after resolution, fetch the previously resolved
  commit directly and fail closed when it is no longer available.
- Add end-to-end `elapsed_seconds` to CLI and Python probe outcomes so operators
  can distinguish acquisition and runner overhead from per-step preparation
  and test timings.
- Make PyPI publication opt-in through the `PYPI_TRUSTED_PUBLISHING`
  repository variable while keeping verified GitHub releases independent.

## 0.3.2

- Allow the checkout-free publish job to address its GitHub repository
  explicitly when creating the release.

## 0.3.1

- Copy committed repository contents into the isolated workspace without
  attempting to preserve the bind mount root's host ownership. This fixes
  Docker execution on native Linux while retaining symlinks and executable
  permission bits.
- Keep native container E2E output visible so runner-policy regressions remain
  diagnosable in CI.
- Bump the execution policy to v15 so evidence produced with the incompatible
  workspace-copy behavior is never reused.

## 0.3.0

- Resolve public GitHub repositories, branches, tags, commit URLs, and pull
  request URLs through isolated credential-free Git rather than an API token.
- Compile root Python, Node.js/Bun/Deno, Rust, and Go baselines, plus explicit
  reviewable monorepo steps from `.gh-freshclone.toml`; mount prepared Python
  and Node.js/Bun state at each step's exact working directory.
- Separate dependency preparation from a network-disabled test container and
  bind execution to content-addressed image identities after refreshing every
  mutable image tag. Execution policy v14 invalidates evidence made before
  this registry-refresh, nested-workspace, verified preparation-cache, and
  image-toolchain-PATH guarantee.
- Add receipt v6, recording canonical CPU and memory limits and verified
  preparation-cache reuse while preventing PASS reuse across different
  resource requests.
- Add bounded repository-scoped dependency caches and receipt/log evidence,
  collision-resistant repository namespaces, per-resource cross-process locks,
  race-free LRU pruning, structured failure diagnostics, and a versioned
  Tsumugi adapter.
- Lazily load planning and execution modules, with a dependency-light console
  entry point and a cross-platform CLI-startup performance gate.
- Support Docker, Podman, and Apple `container`, with native Docker and
  dedicated Apple-container E2E workflows and ready-runner fallback.
- Add a tokenless Trusted Publishing release workflow with distribution smoke
  tests, SHA-256 checksums, GitHub artifact attestations, and a strict
  read-only-build / OIDC-publish job boundary.
- Treat Go's `go` directive as a minimum rather than a preferred runtime:
  quick baselines use the refreshed stable image unless an explicit
  `toolchain` selects a release. Also suppress false nested-manifest warnings
  for declared Cargo workspace members, preserve image-declared toolchain
  paths with non-login shells, and preserve JSON output on initialization
  failures. Go build artifacts remain cached while `-count=1` forces each
  proof to execute tests again.
- Run Bun baselines from a Node LTS image with a pinned, prepared Bun binary,
  allowing common hybrid `bun`/`node`/`npm` test scripts without maintaining a
  custom image. Compile a manifest-declared build before tests when a package's
  declared entrypoint is absent from the checkout.
- Resolve Deno's maintained `debian` tag to a content digest instead of
  assuming that its registry publishes a major-only tag. Keep offline
  enforcement at the container boundary rather than passing an unsupported
  `--cached-only` option to `deno task`.
- Scope preparation caches to dependency and preparation inputs rather than
  the test command, so selector-only fixes reuse downloaded dependencies
  without reusing test results.
- Carry Deno's cache, vendor tree, and node modules through an exact-commit
  prepared volume so dependency preparation survives the phase boundary while
  tests remain network-disabled.
- Classify Deno DNS failures under a network-disabled test policy as an
  actionable environment gap instead of a repository test failure.
- Preserve sampled diagnostic evidence after the output tail, so bounded
  result details cannot discard an actionable error that occurred before a
  long test summary.
