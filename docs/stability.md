# Stability and release policy

`gh-freshclone` is alpha software, but its outputs are intended for automation.
This policy separates interfaces that need deliberate compatibility handling
from internal implementation details that can continue to evolve quickly.

## Public interfaces

The following are public interfaces:

- CLI command names, flags, documented exit codes, and `--json` envelopes;
- Python objects exported from `gh_freshclone.api`;
- `.gh-freshclone.toml` configuration version 1;
- plan, receipt, execution-policy, API, and Tsumugi-adapter version fields;
- the current receipt JSON Schema bundled in distributions;
- GitHub Action inputs and the `result-path` output.

Changes to these interfaces require tests, README and changelog updates, and a
version or explicit compatibility gate where existing evidence could otherwise
be misread. Cache reuse always fails closed across incompatible plan, receipt,
or execution-policy versions. Bundled older schemas are documentation for
existing consumers; they do not make old PASS evidence reusable under a newer
execution policy.

Internal detector functions, cache layout details not documented as public,
and human-readable CLI prose are not compatibility promises.

## Deprecation

When safety permits, a public interface removal should be announced in the
changelog for at least one feature-bearing release before removal. A safety or
correctness issue may require an immediate fail-closed change; its release must
explain the invalidated evidence boundary and bump the relevant compatibility
version.

Alpha releases may still make incompatible changes. Consumers should validate
the version fields they understand instead of accepting unknown fields or
assuming that package SemVer alone identifies evidence semantics.

## Release discipline

- Merge related capability work before publishing one feature-bearing minor
  release; do not publish a new minor release for every merged pull request.
- Avoid multiple feature-bearing minor releases on the same day. Security,
  correctness, and broken-distribution fixes may ship immediately as patches.
- Before a release, require the ordinary CI matrix, native Docker boundary,
  distribution smoke test, performance contract, and public plan cohort to be
  green. The public cohort is an external observation rather than a
  pull-request gate, so transient GitHub acquisition failures must be reviewed
  separately from detector mismatches.
- Keep release notes outcome-focused and group internal policy-version bumps
  with the user-visible behavior that required them.

The project currently supports the latest released minor version. Security
fixes target that version, as described in `SECURITY.md`.
