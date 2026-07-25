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
to untrusted tests; a reviewed `.gh-freshclone.toml` can opt in when that is
the desired baseline.

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

- Rust was planned but not compiled in this trial because it is a substantially
  larger build; it remains covered by unit and container-runner tests.
- macOS command generation and Apple-container CI exist, but this report does
  not claim a physical Mac result. A real Apple silicon run remains required
  before calling macOS field-proven.
- Elysia's separate `test/cloudflare` manifest remains an explicit warning.
  The root quick baseline does not claim to prove that environment.
