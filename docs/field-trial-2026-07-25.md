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
