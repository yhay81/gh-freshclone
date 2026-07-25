# Security policy

## Supported versions

`gh-freshclone` is alpha software. Security fixes target the latest released
minor version and the default branch.

## Reporting

Use GitHub's private vulnerability-reporting flow in the repository Security
tab when it is available. If private reporting is unavailable, open a minimal
issue asking for a private contact channel; do not include an exploit, token,
private-repository content, or sensitive log output in a public issue.

## Boundary

Repository setup and test commands are untrusted code. The tool gives them a
credential-free OCI container with a read-only exact-commit input, bounded
resources, repository-scoped caches, and no test network by default. It also
isolates Git configuration while materializing the checkout, validates OCI
image references again at the execution boundary, and never serializes a
credential-bearing local Git remote into a plan or receipt.
Repository cache namespaces include a hash of their canonical GitHub identity
or local checkout path, preventing sanitized-name collisions from sharing
dependency or prepared-volume state.

This reduces exposure; it does not make a general-purpose OCI runtime an
absolute security boundary. Keep the container runtime and host patched, and
do not add credential, socket, device, or broad host mounts for an untrusted
repository.
