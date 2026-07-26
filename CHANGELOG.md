# Changelog

All notable changes to `gh-freshclone` are documented here. The project uses
semantic versioning while its public receipt, execution-policy, plan, and
adapter versions evolve independently.

## Unreleased

## 0.16.0

- Add fail-closed root Make/configure detection. Planning accepts only a
  bounded UTF-8 `GNUmakefile` or `Makefile` with a literal ordinary `test`,
  `tests`, or `check` target and, when present, a root sh/bash `configure`.
  It never infers commands from README or CI text, variable-expanded targets,
  recipe bodies, or unknown configure entrypoints.
- Run the fixed configure/Make lifecycle in the official multi-architecture
  Buildpack Dependencies Bookworm image with no dependency-preparation phase,
  forwarded credentials, or container network. It shares its base layers with
  the existing Python, Node, and Ruby runtimes instead of adding a dedicated
  2 GiB compiler image. `quick` prefers the conventional `check` target while
  `reproduce`/`full` prefer `test`; on zstd this avoids the initial baseline's
  all-variant 5 GB large-data suite. Supported CMake plans take precedence,
  avoiding duplicate native builds. Plan v9 and execution policy v21 prevent
  reuse across the new selection boundary.
- Add Windows/macOS/Linux materialization coverage, a native Docker
  network-disabled C compilation E2E, and a native arm64 CMake/Make boundary
  job. Replanning 45 recent KAGARI candidates increased automatic coverage
  from 23 to 26 by adding FFmpeg, CUPS, and Redis; targeted checks also added
  Samba and zstd without broadening the command-inference boundary.

## 0.15.0

- Add conservative Ruby/Bundler detection for an exact `Gemfile.lock` with a
  generic `ruby` platform, a pinned `BUNDLED WITH` version, complete Bundler 4
  SHA-256 checksums, and a direct locked RSpec or Rake-backed
  Minitest/test-unit runner. Reject Git/plugin sources, custom gem servers,
  escaping path sources, checksum gaps, architecture-only locks, and
  transitive test runners.
- Download exact `.gem` archives directly from RubyGems in bounded parallel
  transfers and verify every lock-provided SHA-256. This preparation phase
  never evaluates `Gemfile`, gemspecs, Rakefiles, native extensions, or other
  repository code.
- Mount the verified gem set read-only into a second network-disabled
  container. Install the exact locked Bundler into disposable state, perform
  the frozen local bundle install there, compile any native extensions
  offline, and rerun the selected test suite without reusing test results.
  Execution policy v20 prevents reuse of evidence produced before this
  boundary.
- Add Docker/Apple `container` command-contract coverage plus native amd64 and
  arm64 prepare/offline E2Es. A physical
  `faker-ruby/faker@cca4184947e09fdd02afb8b89d25a9c8ebc7274e` proof passed
  2,179 tests and more than 250,000 assertions offline. Its 47 checksummed gem
  archives prepared in 2.063 seconds; an intentionally fresh repeat reused
  only verified dependencies and reran the complete suite.

## 0.14.0

- Add conservative root-level Composer/PHPUnit detection. Automatic plans
  require an exact `composer.lock`, a direct matching `phpunit/phpunit`
  package, and the standard vendor/bin layout; locks missing the declared
  runner, transitive test runners, and custom layouts fail closed with
  explicit warnings.
- Install the locked dependency graph in the official multi-architecture
  Composer 2.10.1 image with plugins and scripts disabled. Persist only the
  scoped Composer cache/vendor state, then mount it read-only, copy `vendor/`
  into a disposable workspace, disable Composer networking, replay repository
  install hooks without a redundant advisory lookup, and run PHPUnit in a
  separate network-disabled workspace.
- Reject every lock containing a Composer plugin rather than executing plugin
  code during the network-enabled phase. Classify explicit PHP version or
  extension incompatibilities as environment gaps. Execution policy v19
  prevents reuse of pre-PHP evidence.
- Cover Docker and Apple `container` command construction plus native amd64
  and arm64 prepare/offline boundaries. A physical
  `mockery/mockery@3a80322e874fbdce4e87e739456fe48d48a527c8`
  proof ran 705 tests and 1,105 assertions offline; an intentionally fresh
  repeat reused only verified dependency state.

## 0.13.0

- Stream the exact committed Git tree into one deterministic tar archive and
  mount that single file into every prepare/test container. This removes the
  per-file Windows/macOS VM bind-copy bottleneck while excluding untracked and
  modified working-tree content by construction.
- Preserve executable modes, symbolic links, and commit timestamps. Rebuild a
  small writable `.git` metadata layer inside each disposable workspace while
  reading immutable objects from the original checkout, so ordinary Git
  version probes keep working without copying object packs.
- Fall back to the previous read-only bind copy when the committed tree cannot
  be archived safely or portably. Execution policy v18 prevents reuse of
  evidence made under the old transport contract.
- In the Windows field trial, an 8,517-file PHPStan checkout took more than
  124 seconds to copy through the Docker bind boundary. Its exact 26.52 MiB
  archive was built in 1.224 seconds and extracted with working Git metadata
  in 2.681 seconds, a greater-than-96% setup reduction.

## 0.12.0

- Add a conservative automatic .NET baseline for repositories with exactly
  one root `.sln` or `.slnx`. Planning parses solution and project XML
  statically, maps supported `global.json` SDK 8/9/10 declarations to official
  multi-architecture Microsoft SDK images, and never evaluates MSBuild.
- Make `quick` choose one ordinary unit-test project while excluding
  benchmark, integration, performance, sample, and utility projects. When the
  chosen project declares a literal framework matching its SDK, execute only
  that TFM rather than every target.
- Persist NuGet packages and restore-generated `obj` inputs in an
  exact-commit, image-scoped managed volume. A separate network-disabled
  container copies only those inputs into a fresh workspace, recompiles, and
  reruns tests without reusing `bin` output.
- Cover Docker and Apple `container` construction plus native amd64 and arm64
  restore/offline-test boundaries. A real AutoMapper proof passed 1,217 tests;
  a repeat reused dependency preparation but reran compilation and tests.

## 0.11.0

- Add a conservative automatic CMake/C++ baseline. Planning statically
  requires a root CTest signal, rejects minimum CMake versions newer than the
  pinned 3.31.10 runtime, and infers at most one ordinary project test option
  while excluding expensive or specialized variants.
- Prepare pinned CMake 3.31.10 and Ninja 1.13.0 in an app-managed volume and
  run a configure-only dependency phase that persists FetchContent sources.
  A fresh build tree is then configured, built with bounded parallelism, and
  tested in a separate network-disabled container. FetchContent is forced
  fully disconnected and zero discovered tests are a failure rather than a
  false PASS.
- Cover Docker and Apple `container` volume construction plus native amd64 and
  arm64 C++ prepare/offline/build/CTest boundaries. A real
  `fmtlib/fmt@2a2d9edb257322bec0f7ac602fde3b382fe0082a` check passed 21 of 21
  tests offline; its exact-commit PASS then returned in 0.42 seconds.

## 0.10.0

- Compile automatic plans from Git tree metadata and only the committed
  detector inputs, without hydrating source and test bodies. Expand the same
  exact-SHA checkout only after `check` finds an executable baseline; explicit
  `.gh-freshclone.toml` layouts retain complete-checkout semantics.
- Avoid Windows checkout failures caused by unrelated NTFS-incompatible paths
  while planning. Unsupported repositories now return ordinary no-baseline
  evidence instead of an initialization error.
- Reduce real cold plan time for `dotnet/runtime` from 106.77 seconds to 2.67
  seconds in the Windows field trial, while preserving Python, Node.js, Rust,
  Go, Maven, and Gradle detection behavior.

## 0.9.0

- Add conservative Maven and Gradle automatic baselines. Both prepare
  dependencies with network access and rerun the selected lifecycle offline in
  a separate credential-free container; committed wrappers and Java toolchain
  declarations participate in evidence and cache identity. Java dependency
  state uses app-managed volumes to avoid host UID/permission coupling without
  widening container capabilities.
- Make Maven offline execution robust to dynamically selected Surefire
  providers and JUnit Platform launcher artifacts that
  `dependency:go-offline` alone omits.
- Select compatible Gradle JDK 17/21/25 images from wrapper and literal
  toolchain evidence, preload test runtime classpaths with a static trusted
  init script, and keep Gradle workers bounded across concurrent repositories.
- Preserve early network, legal-agreement, and Java-toolchain error lines past
  the ordinary log tail. Runtime downloads blocked by the offline policy,
  missing toolchains, and required external agreements now produce actionable
  `ENVIRONMENT_GAP` diagnostics rather than generic test failures.

## 0.8.0

- Compile conservative Python baselines for committed `setup.py`/`setup.cfg`
  projects without executing their packaging code during planning. Declared
  pytest extras and common legacy development/test requirements files are
  installed only inside the network-enabled preparation container.
- Put the prepared Python virtual environment first on `PATH` during the
  offline test phase, so repository tests that spawn `python` or installed
  console scripts remain in the same isolated dependency environment.
- Include legacy Python packaging and requirement inputs in dependency
  identity, and cover the child-process boundary with native runner E2E.

## 0.7.0

- Add `github-status`, an explicit read-only GitHub REST API integration that
  resolves an exact public commit and reports its GitHub Checks, legacy commit
  statuses, repository identity, and visible API quota without executing
  repository code or changing baseline cache identity.
- Normalize an empty legacy status collection to `none` instead of GitHub's
  API-level `pending`, retain the original API state, and refuse to claim
  complete success when more than 100 latest check runs are present.
- Document the production/development integration, public-data boundary, and
  support contact needed for the GitHub Developer Program.

## 0.6.1

- Enforce the prepared-volume byte and count limits immediately after a
  preparation cache miss, even when the normal daily maintenance interval has
  not elapsed. Cache hits avoid the volume-usage probe, and concurrent
  processes share one automatic-maintenance lock.

## 0.6.0

- Make test-container network access an operator-controlled policy. Every plan
  and check is offline by default even when repository-owned configuration
  requests network; `--test-network enabled` is the explicit opt-in for suites
  that download assets while testing.
- Record the effective network policy in plan and receipt identity, separate
  offline and network-enabled PASS indexes and single-flight locks, and safely
  retain pre-clone reuse of legacy offline PASS evidence without accepting
  legacy network-enabled evidence.
- Add Debian package hints for common missing browser shared libraries. A real
  network-enabled Deno Fresh run now distinguishes its runtime downloads from
  the next environment gap, `libgobject-2.0.so.0`, and suggests
  `libglib2.0-0`.

## 0.5.1

- Bound runner readiness, version, image/volume metadata, prepared-volume
  creation, failure diagnostics, and cleanup control-plane calls to 15 seconds.
  Probe independent installed runners concurrently while preserving preference
  order. Image downloads and repository prepare/test phases remain unbounded
  by this control deadline.
- Discard only the repo/commit/lockfile/image-scoped dependency cache after a
  failed preparation phase, preventing interrupted or storage-corrupted package
  state from poisoning every retry. Perform one clean preparation retry in the
  same check, leave the scoped cache clean after a repeated preparation
  failure, and preserve prepared dependencies after ordinary test failures.

## 0.5.0

- Measure app-owned Docker and Podman prepared-volume usage and enforce a
  3 GiB byte budget in addition to the existing count and age limits. Expose
  the measured bytes through `cache status` and add `cache prune
  --max-volume-gib`.
- Preserve a 2 GiB host-filesystem reserve before fresh execution. When space
  is low, reclaim only app-owned cache first and fail before cloning if the
  reserve cannot be recovered; immutable PASS receipt reuse remains available.
- Make Windows cache cleanup recover read-only and concurrently disappearing
  Linux-created nodes, extended-length paths, and transient non-empty
  directories. Report runner-side volume-removal failures instead of silently
  claiming a successful prune.
- Bound cache metadata calls to a stopped runner, replace per-volume
  availability probes with one label-filtered discovery, label execution
  containers, and explicitly attempt named-container cleanup after every
  non-zero runner exit.
- Diagnose a read-only container metadata store as an actionable runner
  storage failure rather than a generic infrastructure error.
- Complete the six-ecosystem physical field trial with a real ripgrep Rust
  workspace PASS.

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
- Remove named Apple containers when a default SIGTERM interrupts execution,
  and mount `/tmp` as tmpfs with Apple `container`. Execution policy v16
  prevents reuse of evidence produced without these guarantees.
- Expand native runner E2E coverage to verify the network-disabled interface,
  read-only committed input, tmpfs, native Apple guest architecture,
  whitespace/Unicode host paths, and interruption cleanup.

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
