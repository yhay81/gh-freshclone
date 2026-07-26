# Public-repository field trial — 2026-07-25

This field trial used the installed `gh-freshclone` CLI against immutable
commits from six active public repositories. It was not a fixture-only test.
The host was Windows 11 with Docker 29.6.1, 4 CPUs, and an 8 GiB container
limit. No GitHub token or repository credentials were supplied.

## Plan coverage

Final plans were compiled from exact commits. Planning does not execute
repository code.

| Repository | Commit | Ecosystem | Plan time | Result |
|---|---:|---|---:|---|
| `pallets/itsdangerous` | `672971d66a2e` | Python/tox/uv | 2.477 s | executable plan |
| `sindresorhus/yocto-queue` | `b07eac099753` | Node/npm | 1.792 s | executable plan |
| `BurntSushi/ripgrep` | `f9c05a949d1a` | Rust/Cargo | 2.326 s | executable plan |
| `stretchr/testify` | `001eb7946baf` | Go | 1.971 s | executable plan |
| `denoland/fresh` | `86d6cdeb331a` | Deno | 2.977 s | executable plan |
| `elysiajs/elysia` | `8358ff9efbce` | Bun + Node/npm | 3.735 s | executable plan; one intentionally uncompiled nested Cloudflare baseline |

The Rust root step correctly covers the declared Cargo workspace without
claiming that each member needs a separate configuration.

## Execution observations

All test phases below ran in a second container with `network=none`.
Dependency preparation was the only network-enabled phase.

| Repository | Observation | Total time | Runner time | Preparation |
|---|---|---:|---:|---:|
| `yocto-queue` | cold PASS, 7 tests | 33.74 s | 28.631 s | 23.817 s |
| `yocto-queue` | warm PASS, tests rerun | 10.67 s | 5.600 s | 0.895 s, cache hit |
| `itsdangerous` | PASS, 297 tests | 13.33 s | 8.950 s | 5.039 s |
| `testify` | cold PASS on stable Go | 162.51 s | 137.741 s | 6.718 s |
| `testify` | warm PASS, `-count=1` still reruns tests | 53.56 s | 45.700 s | 8.969 s, cache hit |
| `testify` | immutable PASS receipt reuse | 1.333 s | no container | no preparation |
| `elysia` | cold PASS: build, 1,525 Bun tests, type check, CJS and ESM Node checks | 102.28 s | 69.945 s | 37.388 s |
| `elysia` | warm PASS with the same checks | 41.93 s | 33.761 s | 6.725 s, cache hit |
| `fresh` | 307 tests pass; 16 tests require runtime downloads | 42.45 s warm | 38.161 s | 11.245 s, cache hit |

`fresh` is intentionally reported as `ENVIRONMENT_GAP`, not as a repository
failure. Its test suite downloads Chrome metadata, npm content, and a JSR WASM
asset at runtime. Automatic detection does not silently grant network access
to untrusted tests; the operator can opt in explicitly when that is the
desired baseline.

### Operator-controlled test network — 2026-07-26

The first field trial exposed an awkward policy boundary: automatic plans
correctly kept Fresh offline, while a repository-owned configuration could
grant its own test process outbound access and an external operator had no CLI
override for an otherwise detected plan. Version 0.6.0 makes the caller the
sole policy authority. `plan` and `check` force every test step offline by
default; `--test-network enabled` is an explicit opt-in recorded in the plan,
receipt, PASS index, and single-flight identity.

A native Docker boundary probe used a committed configuration that requested
network. The default check observed no `eth0`; the explicit opt-in observed
`eth0`. Both passed, while their receipts and pre-clone PASS indexes remained
distinct. A legacy offline PASS remains reusable, but a legacy
network-enabled PASS cannot satisfy an offline request.

The same opt-in was then exercised against
`denoland/fresh@86d6cdeb331a719cf8b1c85bf5e43c8ffa889b3b`. The effective
plan changed only `test_network` and its operator-policy warning; its
dependency fingerprint stayed identical. The 46.097-second check reused the
verified preparation, enabled the test network, and moved past the previous
download-policy failure. It then returned the next accurate
`ENVIRONMENT_GAP`: downloaded Chrome could not load
`libgobject-2.0.so.0` from the Deno Debian image. The runner took 42.659
seconds, including a 10.763-second preparation cache hit. Structured
diagnostics identify the missing shared library and suggest Debian package
`libglib2.0-0`; the tool does not silently mutate the image.

## Physical Apple-container validation

A native run was completed on an Apple silicon Mac with macOS 26.5.1,
Apple `container` 1.1.0 (commit `5973b9c`), 4 CPUs, and an 8 GiB container
limit. The container system was started from a stopped state, and `doctor`
reported the Apple runner as supported and ready.

The initial dedicated native E2E suite passed both probes in 125.63 seconds,
including the first image pulls:

- a committed Python fixture passed through distinct network-enabled
  preparation and network-disabled test containers;
- a committed nested pnpm fixture passed, then passed again with the same
  prepared volume and `prepare_cache_hit=true`;
- both probes recorded `test_network=none`, and their app-owned test volumes
  were removed after the run.

The public Node trial was then repeated against the same immutable
`sindresorhus/yocto-queue` commit used above,
`b07eac099753833b29d06c614149904445739776`:

| Observation | Wall time | Runner time | Preparation |
|---|---:|---:|---:|
| Apple-container cold PASS, 7 tests | 25.04 s | 20.460 s | 17.382 s, cache miss |
| Apple-container warm PASS, tests rerun | 7.25 s | 4.096 s | 0.998 s, cache hit |
| Immutable PASS receipt reuse | 0.50 s | no container | no preparation |

The cold and warm executions both used the digest-pinned Node image, separate
phase containers, and a network-disabled test phase. This closes the
physical-Mac validation gap; the earlier Windows and native Apple-container
measurements are development observations rather than cross-platform
performance guarantees.

### macOS hardening follow-up — 2026-07-26

A real SIGTERM cancellation probe found that the existing execution policy
stopped the Apple VM but could leave its named container behind. Execution
policy v16 turns the default SIGTERM action into a cleanup-capable exit, stops
and deletes the named container, and mounts `/tmp` as tmpfs on Apple
`container`.

The expanded native suite passed all three probes in 25.12 seconds with warm
images. It now verifies runtime behavior rather than only generated flags and
receipt fields:

- the offline test container exposes only the loopback network interface;
- the committed `/input` mount rejects a write attempt;
- `/tmp` is a tmpfs mount;
- the Linux guest runs natively as `aarch64`;
- host checkout and temporary paths containing spaces and Japanese characters
  complete successfully;
- SIGTERM exits with status 143 and leaves no named Apple container.

## Defects found and corrected

1. A Go minimum version was incorrectly treated as the preferred runtime and
   combined with a Debian tag that did not exist. Automatic Go now uses the
   refreshed stable image unless an explicit `toolchain` selects a release.
2. Login shells erased image-defined toolchain paths such as
   `/usr/local/go/bin`. Phase containers now use a non-login shell while still
   bypassing image entrypoints.
3. Go's persistent build cache could also reuse successful test results.
   `go test -count=1` keeps compilation fast but executes tests for every fresh
   proof.
4. Bun's official image lacked Node/npm for a real hybrid suite. A pinned Bun
   binary is now prepared in a Node LTS image, avoiding a project-maintained
   custom image.
5. A generated package entrypoint was missing until `build` ran. When a
   manifest-declared entrypoint is absent, a known build script is compiled
   before the test script.
6. Deno's assumed major-only image tag did not exist, and `--cached-only` was
   not a valid `deno task` argument. The maintained Debian tag is resolved to
   a digest and offline enforcement remains at the container boundary.
7. Deno vendor and module state disappeared between preparation and test
   containers. Exact-commit prepared volumes now carry `DENO_DIR`, `vendor`,
   and `node_modules` across the boundary.
8. Test-command-only changes unnecessarily invalidated dependency state.
   Preparation identity now excludes test behavior while retaining the
   dependency fingerprint, image digest, preparation command, and working
   directory.
9. Long output could discard an earlier actionable network error. Diagnostic
   evidence is retained independently of the bounded output tail.
10. `--json` initialization failures emitted plain text. They now return a
    stable JSON error envelope with exit code 2.

These corrections are encoded in execution policy v14 so older evidence and
prepared state cannot be mistaken for current proofs.

## Product feedback

- The core value proposition held up: pasting a repository name produced an
  exact-commit, credential-free baseline without installing project
  dependencies on the host.
- Warm preparation materially changes usability for repeated agent work:
  Node improved from 33.74 s to 10.67 s, Go from 162.51 s to 53.56 s, and Bun
  from 102.28 s to 41.93 s.
- A PASS receipt is useful for orchestration: the same immutable Go proof
  returned in 1.333 s.
- Failure taxonomy matters as much as PASS rate. The Deno run demonstrated
  that a safe network policy can stop a suite while still giving the operator
  an actionable, non-accusatory result.
- Minimum-runtime compatibility and flaky timing checks are different from a
  quick current baseline. Optional flake confirmation or compatibility
  profiles may be valuable later, but automatic retries must not hide a
  nondeterministic repository.

## Remaining validation

- Elysia's separate `test/cloudflare` manifest remains an explicit warning.
  The root quick baseline does not claim to prove that environment.

### Rust completion — 2026-07-26

The previously deferred Rust target was completed against
`BurntSushi/ripgrep@f9c05a949d1a0dc8e16dee28ca9605d38611faeb`.
The digest-pinned `rust:bookworm` preparation container fetched the locked
dependencies, then a separate network-disabled container passed
`cargo test --offline --workspace`.

| Observation | End-to-end | Runner | Preparation |
|---|---:|---:|---:|
| cold PASS | 242.776 s | 179.974 s | 109.685 s, cache miss |
| warm PASS with tests rerun | 62.219 s | 56.016 s | 3.984 s, cache hit |

This closes the physical execution gap across the six detected ecosystems.

## Storage-pressure follow-up

A forced Deno rerun exposed a host with only about 5 MiB free on the filesystem
holding Docker Desktop and the default app cache. Docker's metadata database
became read-only after dependency preparation and left the client unable to
remove its container or old prepared volumes. At that point gh-freshclone had
18 prepared volumes using about 4.3 GiB and 2.75 GiB of host dependency cache.
The count-only limit was therefore insufficient even though it had not reached
24 volumes.

Version 0.5.0 adds measured prepared-volume bytes, a 3 GiB default volume
budget, a 2 GiB pre-execution host reserve, robust Windows cleanup of
Linux-created cache trees (including a real 270-character Cargo path), bounded
runner metadata calls, visible runner-removal warnings, non-zero execution
cleanup, and a specific read-only runner-storage diagnostic. The repaired
pruner removed 1,890,663,264 bytes of the previously undeletable Rust cache and
reduced prepared volumes from 4,464,499,998 to 2,928,699,999 bytes by removing
only the eight oldest app-owned volumes. Both operations completed with no
warnings. The reserve is checked only after an immutable PASS miss, so low disk
does not disable the fastest cached proof.

### Continuous prepared-volume bound — 2026-07-26

A later runtime audit found 13 app-owned prepared volumes using 3,500,699,996
bytes. The daily maintenance marker was only 14.2 hours old, so the automatic
path returned before measuring usage even though the 3 GiB hard limit had
already been crossed. The pre-execution free-space reserve still protected the
host from exhaustion, but the configured cache budget was temporarily soft.

Version 0.6.1 records whether dependency preparation actually missed its cache.
After such a miss, it measures only the growth-prone prepared-volume category
and bypasses the daily interval when the byte or count limit is exceeded.
Preparation hits retain the zero-probe fast path. A cross-process maintenance
lock prevents two finishing checks from running the same prune concurrently.

The corrected automatic path was applied to the live cache with no execution
containers active. It removed the two oldest app-owned Bun volumes and reduced
usage to 2,766,299,996 bytes across 11 volumes, below the 3 GiB budget. No
unmanaged volume or host path was selected; the removed dependency state is
recreated on demand.

### Failed-preparation cache recovery — 2026-07-26

The original storage incident also left Deno package-manager state partially
written in its exact-commit prepared volume. Two later forced checks reproduced
the same prepare-phase JSON parse error even though Docker and host storage had
recovered. The success marker correctly prevented a false cache hit, but the
next prepare still consumed the poisoned partial cache.

Version 0.5.1 discards only the failing
repo/commit/lockfile/image-scoped dependency cache after a non-zero preparation
phase and retries preparation once in the same check. A repeated preparation
failure returns without another retry and leaves the scoped cache clean. The
existing poisoned `denoland/fresh@86d6cdeb331a` volume provided a physical
recovery test while this behavior was developed:

| Attempt | Observation |
|---|---|
| poisoned retry | same parse error; app removed the one scoped volume and its ledger record |
| clean retry | preparation completed; test reached the expected `environment_gap` for network access under the offline policy |
| repeated test | `prepare_cache_hit=true`; the valid volume remained available after the test-phase failure |

The final implementation folds the first two observations into one check.
This distinguishes disposable partial installation state from successfully
prepared dependencies and prevents an infrastructure incident from becoming a
persistent false repository failure or an unbounded retry loop.

A final physical fault injection replaced the managed volume's `DENO_DIR`
directory with a regular file after verifying its gh-freshclone ownership
labels. One v0.5.1 check then produced two preparation headers, one discard
marker, one clean-retry marker, and the correct test-phase `environment_gap`.
The combined runner duration was 68.333 seconds with 21.270 seconds attributed
to both preparation attempts. The recreated prepared volume existed exactly
once in both Docker and the app ledger after completion.

## Exact-target materialization follow-up

The first KAGARI integration run exposed that an exact SHA still entered
`git clone`, which retained default-branch history even though planning only
needs the selected committed tree. The materializer now initializes an empty
repository and fetches only the resolved ref or SHA at depth one.

Two exact public targets were measured immediately before and after the change
on the same Windows host. Elapsed time is an observed development value rather
than a cross-network guarantee; retained commit count and `.git` bytes directly
show the reduced materialization scope.

| Repository | Before | After | `.git` reduction |
|---|---:|---:|---:|
| `pallets/itsdangerous@672971d66a2e` | 1.773 s, 677 commits, 664,670 bytes | 1.123 s, 1 commit, 105,116 bytes | 84.2% |
| `denoland/fresh@86d6cdeb331a` | 2.557 s, 1,778 commits, 5,477,319 bytes | 1.847 s, 1 commit, 1,897,463 bytes | 65.4% |

Credential isolation, blob filtering, detached checkout of the previously
resolved commit, and fail-closed behavior remain unchanged. Probe JSON now
records end-to-end `elapsed_seconds`, allowing future KAGARI evidence to
separate acquisition overhead from receipt step timings.

## GitHub status integration — 2026-07-26

The Developer Program completion audit found that the baseline compiler used
GitHub's smart HTTP protocol but did not yet integrate with the GitHub API.
The missing API surface also represented a product gap: a clean local PASS and
the upstream CI state for the same exact commit had to be inspected with
different tools.

Version 0.7.0 adds an explicit, read-only `github-status` command. A physical
unauthenticated REST call against
`yhay81/gh-freshclone@d3b5e35b689b82036e8c0d849c488e6f9e794740`
returned 21 completed GitHub Checks, all successful, in about two seconds.
The legacy combined-status endpoint returned `pending` with zero contexts, a
GitHub API convention that initially made the combined observation falsely
pending. The implementation now preserves that raw API state while treating
the empty evidence set as `none`; the exact commit correctly reports overall
`success`.

The final command uses two requests rather than a separate repository metadata
request: the combined-status response already contains canonical repository
identity. It exposes the remaining public API quota, does not retry rate-limit
responses, bounds each response to 5 MiB and 15 seconds, and marks more than
100 latest check runs as partial rather than claiming complete success. The
mutable CI context is not part of deterministic plan, receipt, or PASS-cache
identity.

## Legacy Python baseline follow-up — 2026-07-26

A second public-repository plan trial sampled ten current default-branch
commits across Python, Node.js, Go, and Rust:

| Repository | Commit | Ecosystem | Plan time | Warnings |
|---|---:|---|---:|---:|
| `astral-sh/ruff` | `a5cdc6d5813b` | Rust | 16.67 s | 1 nested-manifest warning |
| `cli/cli` | `592255318aa6` | Go | 4.18 s | 0 |
| `fastapi/fastapi` | `255b91292890` | Python | 6.28 s | 0 |
| `vitejs/vite` | `3ac77d9dd742` | Node.js | 5.03 s | 1 nested-manifest warning |
| `nektos/act` | `4f411281417e` | Go | 5.66 s | 0 |
| `sharkdp/bat` | `78951393e29b` | Rust | 3.09 s | 0 |
| `psf/requests` | `69f84847045b` | Python | 2.12 s | 0 |
| `eslint/eslint` | `588a26ddce3c` | Node.js | 4.93 s | 1 nested-manifest warning |
| `pydantic/pydantic` | `a2a6577d4c32` | Python | 3.27 s | 1 nested-manifest warning |
| `httpie/cli` | `5b604c37c6c6` | Python | 2.08 s | 0 |

The first run detected nine of ten. `httpie/cli` was rejected despite its
root `setup.py`, declarative `setup.cfg`, pytest configuration, tests, and
`test` extra. The detector recognized `setup.cfg` as a test signal but had an
earlier reachability guard requiring `pyproject.toml` or a narrowly named
requirements file.

Version 0.8.0 accepts a committed `setup.py` as legacy packaging evidence and
reads only declarative `setup.cfg` metadata during planning. Packaging code is
not imported or executed on the host. The project and selected pytest extra
are installed in the network-enabled preparation container, and tests still
run in a distinct network-disabled container. It also recognizes both
`requirements-dev.txt`/`requirements-test.txt` and the common reversed names
`dev-requirements.txt`/`test-requirements.txt`. The repeated matrix compiled
all ten targets.

The first physical legacy execution used
`fabric/fabric@ded51893f02c33d2bc7c157624c44a039a952037`. Dependency
preparation installed the project and its pinned `dev-requirements.txt`; 393
tests passed, but one test-spawned `python -m fabric` escaped to the image's
system Python and could not see the prepared dependencies. Putting
`/prepared/venv/bin` first on `PATH` closed the child-process boundary. The
same exact commit then passed 394 tests with 7 skips in 10.354 seconds
end-to-end, including a 1.577-second verified preparation-cache hit and a
7.098-second offline runner phase.

`httpie/cli` reached execution as well: 769 tests passed, while its unpinned
legacy test extra resolved to contemporary dependencies that produced 205
failures and 26 fixture errors. Some failing paths attempted DNS under the
offline policy and were stopped. This is useful failure evidence rather than
a false PASS; it also demonstrates why immutable source alone cannot make an
unlocked historical dependency graph reproducible.

## Maven and Gradle baseline follow-up — 2026-07-26

An expanded plan trial used 16 current public repositories spanning large
polyglots, framework cores, build systems, and projects outside the automatic
support boundary:

| Repository | Initial automatic result | Java follow-up |
|---|---|---|
| `grafana/grafana` | Node.js + Go | unchanged |
| `vercel/swr` | Node.js | unchanged |
| `kubernetes/kubernetes` | Go | unchanged |
| `hashicorp/vault` | Go | unchanged |
| `facebook/react` | Node.js | unchanged |
| `protocolbuffers/protobuf` | no root baseline | unchanged |
| `OpenPrinting/cups` | no root baseline | unchanged |
| `signalapp/libsignal` | Rust | unchanged |
| `apache/logging-log4j2` | no root baseline | Maven |
| `spring-projects/spring-framework` | no root baseline | Gradle |
| `jenkinsci/jenkins` | Node.js only | Node.js + Maven |
| `rails/rails` | no root baseline | unchanged |
| `laravel/framework` | no root baseline | unchanged |
| `php/php-src` | no root baseline | unchanged |
| `ruby/ruby` | Rust evidence | unchanged |
| `wordpress/wordpress-develop` | Node.js | unchanged |

Adding conservative Java compilation expanded useful root detection from 9 of
16 to 11 of 16 without guessing C/C++, PHP, or Ruby commands. Additional
immutable Java plans covered `apache/commons-lang`, wrapper-based
`apache/logging-log4j2`, mixed `jenkinsci/jenkins`, wrapper-based
`spring-projects/spring-framework`, and dual-build
`spring-projects/spring-petclinic`. Physical Gradle iterations used these exact
targets:

| Repository | Commit | Observation |
|---|---:|---|
| `junit-team/junit5` | `4782f9e4e4b5` | exposed JDK and missing-`git` image assumptions |
| `diffplug/spotless` | `3924217d5f71` | reached the offline phase; plugin configuration required remote metadata |
| `ben-manes/gradle-versions-plugin` | `743b5c95a9ae` | reached offline tests; TestKit attempted other Gradle distribution downloads |
| `spring-projects/spring-petclinic` | `f182358d02e4` | Maven and Gradle both passed in `full` |

Planning reads only committed POM, wrapper, build, and wrapper-properties
files. It does not invoke Maven, Gradle, or a wrapper on the host. A committed
Gradle wrapper is mandatory. For a dual Maven/Gradle checkout, quick and
reproduce select one build deterministically while `full` runs both.

Physical execution exposed several defects that fixture-only testing had not:

1. Maven's `dependency:go-offline` did not fetch Surefire's dynamically chosen
   JUnit Platform provider or a matching launcher. The first Petclinic offline
   phase therefore stopped before tests. Version 0.9.0 prefetches those runtime
   artifacts during preparation; the same immutable commit then ran 69 tests
   with zero failures or errors and two skips under network isolation.
2. A Temurin-only Gradle image omitted `git`, which real build metadata used.
   Official Gradle images supply the ordinary build tool environment and have
   both `linux/amd64` and `linux/arm64` manifests for the selected JDK tags.
3. A root task resolving subproject configurations violated Gradle's project
   lock, and configuration-cache serialization rejected the injected closure.
   The final static init script registers one resolver task per project,
   aggregates them only after evaluation, and disables configuration-cache
   reuse for preparation. Repository test methods still execute only in the
   second container.
4. Wrapper version alone is not a Java requirement. Petclinic uses Gradle
   9.5.1 but explicitly declares a Java 17 toolchain; selecting JDK 25 from the
   wrapper caused a deterministic preparation failure. Literal Java 17, 21,
   and 25 toolchain evidence now overrides the compatible wrapper default and
   participates in the plan digest.
5. Some Gradle tests intentionally download other Gradle distributions, some
   plugins parse remote metadata during configuration, and Build Scan can
   require external Terms of Use. The tool does not silently grant test
   network or accept an agreement. Early diagnostic lines are retained beyond
   the ordinary output tail and classified as `network_policy`,
   `missing_java_toolchain`, or `external_agreement_required`.

The final strict physical proof ran
`spring-projects/spring-petclinic@f182358d02e4` with `profile=full`. Maven
`verify` passed from a network-disabled container in 100.225 seconds after a
3.129-second verified preparation-cache hit. Gradle then selected the
repository's declared Java 17 toolchain, completed cold dependency preparation
in 534.838 seconds, and passed `check` offline in 790.727 seconds of total
runner time. End-to-end acquisition and both build systems took 897.90 seconds.
No execution container remained afterward.

An immediately repeated `--no-cache` proof reused only verified dependency
preparation; it did not reuse the PASS receipt or test outputs. Maven
preparation took 4.405 seconds and reran `verify` in 112.919 seconds. Gradle
preparation fell from 534.838 to 2.112 seconds and reran `check` in 195.709
seconds. End-to-end time fell to 318.27 seconds, a 64.6% reduction while both
test phases remained network-disabled.

The native runner suite also creates a minimal committed Maven/JUnit project.
It performs a cold dependency preparation and then passes one JUnit test in a
second network-disabled container. The complete Docker E2E suite finished with
5 passed and the Apple-only lifecycle probe skipped in 265.87 seconds.

## Manifest-only planning follow-up — 2026-07-26

A further 20-repository cohort showed that automatic `plan` still hydrated the
complete worktree even though detection reads a small, fixed set of committed
manifests. This made unsupported large repositories needlessly expensive and
made plan availability depend on whether every unrelated Git path could be
represented by the host filesystem.

Version 0.10.0 now fetches the immutable commit with `blob:none`, enumerates its
Git tree, and checks out only root detector inputs, declared Node entrypoints,
and nested-manifest evidence. The existence of Python test directories is
derived from the committed tree. A `check` with executable steps force-expands
the same checkout to the complete exact commit before any container starts. A
plan with no steps returns without hydrating source blobs. Configured layouts
fall back to a complete checkout because their paths and dependency files are
intentionally repository-defined.

The following cold Windows measurements used current default-branch commits
and no GitHub token:

| Repository | Before | After | Result after change |
|---|---:|---:|---|
| `systemd/systemd` | 1.71 s, initialization error | 2.68 s | normal no-baseline plan |
| `dotnet/runtime` | 106.77 s | 2.67 s | normal no-baseline plan |
| `openssl/openssl` | 20.40 s | 1.60 s | normal no-baseline plan |
| `videolan/vlc` | 12.49 s | 3.06 s | Rust |
| `openpgpjs/openpgpjs` | 2.27 s | 2.59 s | Node.js |
| `projectdiscovery/nuclei` | 4.14 s | 2.55 s | Go |
| `apache/logging-log4j2` | not in initial timing cohort | 4.18 s | Maven |
| `spring-projects/spring-framework` | not in initial timing cohort | 4.13 s | Gradle |

`systemd/systemd` previously failed because two unrelated fuzz corpus filenames
contain colons, which NTFS cannot materialize. Git tree inspection does not
need those paths, so the repository now produces the intended unsupported
baseline result on Windows. Detection parity was checked across Rust, Node.js,
Go, Maven, and Gradle public repositories. The Docker E2E suite then exercised
the two-stage path physically: the partial checkout expanded before execution,
and all five runnable cases passed with the Apple-only case skipped in 128.20
seconds.

## CMake baseline follow-up — 2026-07-26

The largest useful unsupported family in a fresh public-repository cohort was
C/C++ with root CMake metadata. The automatic baseline stays static during
planning: it reads the committed root `CMakeLists.txt`, optional presets and
package manifests, but never evaluates CMake on the host. A literal
`include(CTest)`, `enable_testing()`, or `add_test()` signal is required.
Declared minimum versions newer than pinned CMake 3.31.10 return an explicit
configuration warning instead of guessing a runtime.

Many projects gate tests behind their own option rather than only
`BUILD_TESTING`. The detector therefore selects at most one literal
`option(...)` whose name and description identify an ordinary test build. It
rejects specialized CUDA, conformance, sanitizer, benchmark, integration,
system, and performance variants. The resulting ten-repository manifest-only
trial was:

| Repository | Automatic result | Selected project test option |
|---|---|---|
| `fmtlib/fmt` | CMake | `FMT_TEST` |
| `gabime/spdlog` | CMake | `SPDLOG_BUILD_TESTS` |
| `catchorg/Catch2` | CMake | standard `BUILD_TESTING` path |
| `google/googletest` | CMake | standard path |
| `nlohmann/json` | CMake | `JSON_BuildTests` |
| `CLIUtils/CLI11` | CMake | standard `BUILD_TESTING` path |
| `jarro2783/cxxopts` | CMake | `CXXOPTS_BUILD_TESTS` |
| `protocolbuffers/protobuf` | CMake | `protobuf_BUILD_TESTS` |
| `curl/curl` | rejected | no literal root CTest signal |
| `eclipse-openj9/openj9` | rejected | no literal root CTest signal |

Preparation first installs fixed CMake 3.31.10 and Ninja 1.13.0 artifacts into
an exact-commit, image-scoped managed volume. A configure-only pass may fetch
declared FetchContent sources into the same volume. The second container uses
a fresh build tree, configures with Ninja and
`FETCHCONTENT_FULLY_DISCONNECTED=ON`, builds with two workers, and runs CTest
with two workers, no network, and `--no-tests=error`. Compiled outputs are not
reused. The common official Python/bookworm build image supplies the ordinary
GCC/G++/Make/Git environment without the first-use and storage cost of a large
devcontainer image.

The native Docker E2E first exposed that the CMake wheel's launcher needs its
target directory on `PYTHONPATH`; preserving only its `bin` directory was
insufficient across containers. After scoping both paths to the same prepared
volume, the committed minimal C++ fixture fetched an immutable, SHA-256-checked
fmt source archive during preparation, configured from that retained source
offline, compiled, and passed one of one CTest case.

The non-fixture proof used
`fmtlib/fmt@2a2d9edb257322bec0f7ac602fde3b382fe0082a`. Its exact source expanded
only after planning found the CMake baseline. Fixed tool preparation took
9.97 seconds without a prepared-volume cache hit. The second,
network-disabled container configured, built, and passed all 21 CTest cases in
156.65 seconds; complete acquisition and execution took 175.09 seconds. No
test network opt-in or repository credential was supplied. Repeating the
ordinary exact-commit check reused the PASS receipt and returned in 0.42
seconds.

## .NET solution baseline follow-up — 2026-07-26

A manifest-only cohort measured four current public repositories with one
root solution. Planning parsed committed `.sln`/`.slnx`, project paths, and
`global.json` without evaluating MSBuild:

| Repository | Selected quick project | SDK image |
|---|---|---|
| `App-vNext/Polly` | `test/Polly.Core.Tests/Polly.Core.Tests.csproj` | `10.0.302` |
| `AutoMapper/AutoMapper` | `src/UnitTests/AutoMapper.UnitTests.csproj` | `10.0` |
| `serilog/serilog` | `test/Serilog.Tests/Serilog.Tests.csproj` | `10.0` |
| `FluentValidation/FluentValidation` | `src/FluentValidation.Tests/FluentValidation.Tests.csproj` | `10.0` |

The detector excluded benchmark, integration, performance, sample, testing
utility, and test-app projects. When a selected project contained a literal
TFM matching the SDK, quick mode selected only that framework. Preparation
restored NuGet packages with network access and saved only restore-generated
`obj` state in an exact-commit managed volume. The separate test container
copied those inputs into a fresh workspace, stayed offline, and generated new
`bin` output.

The native fixture restored xUnit packages, passed one test offline, then
performed an intentionally fresh proof. The second proof reported a verified
preparation-cache hit but compiled and ran the test again.

The physical public proof used
`AutoMapper/AutoMapper@dfa6dd587c5854b4beee5934beb39ba6e9569b84`.
Fresh preparation took 44.578 seconds. The offline `net10.0` phase compiled
AutoMapper and passed 1,217 of 1,217 tests; total runner time was 72.666
seconds. An immediate `--no-cache` repeat reused only verified restore state,
then recompiled and reran all 1,217 tests in 28.063 seconds.

`serilog/serilog@07d39cfb2928076ecd902a61d295f90d74fe1fa5`
provided negative evidence rather than a false environment success. Its
network-enabled restore deterministically failed because repository policy
treats `NU1903` as an error and a pinned cryptography package matched five
current high-severity advisories. The app retried once from a clean volume,
observed the same failure, and discarded the failed preparation state.

## Exact workspace archive follow-up — 2026-07-26

The next unsupported-ecosystem trial exposed a cross-cutting transport
bottleneck before dependency preparation began. A fixed
`phpstan/phpstan-src@e68b4013a0c4c4ab3d9c208e5b07254471771a4d`
checkout contains 8,517 tracked files. Copying those files individually from
the Windows host through Docker Desktop's bind boundary was still running
after 124 seconds, both with and without the `.git` object directory.

The replacement transport enumerates the exact commit tree and streams blobs
through one `git cat-file --batch` process into a deterministic POSIX tar. It
does not read working-tree file bodies, so line-ending conversion, a modified
tracked file, and untracked dependency outputs cannot change the container
input. Executable modes, symbolic links, and the commit timestamp are retained.
Every phase extracts the same single read-only archive. A minimal writable
`.git` directory copies only HEAD, index, refs, and configuration, then uses
Git's alternates mechanism to read objects from the original read-only input.

The exact archive was 26.52 MiB and took 1.224 seconds to build. Extracting all
8,517 paths, reconstructing Git metadata, resolving the same commit, and
enumerating the same index took 2.681 seconds. The combined 3.905-second setup
is at least 96.8% faster than the observed per-file copy lower bound. A
deliberately generic filesystem archive reached 156.44 MiB because it included
an untracked partial `vendor/` tree; the exact Git-object archive excluded that
state, providing a security improvement as well as the performance gain.
