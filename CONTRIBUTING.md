# Contributing

Thanks for helping make `gh-freshclone` a trustworthy pre-edit baseline
primitive for OSS contributors and coding agents.

## Before opening a change

- Search existing issues and pull requests.
- Open an issue before a large new ecosystem, public-interface change, or
  dependency addition.
- Never post tokens, credential-bearing remotes, private-repository content,
  or unredacted private logs.
- Keep the runtime dependency-free unless the benefit clearly outweighs the
  installation and supply-chain cost.

## Development

Install the locked development environment:

```shell
uv sync --locked
```

Run the required gates:

```shell
uv run pytest -q
uv run ruff check .
uv run pyright
uv run bandit -r gh_freshclone scripts/github_action.py -q
uv run coverage run -m pytest -q
uv run coverage report
```

Native Docker or Podman changes should also run:

```shell
GH_FRESHCLONE_E2E_RUNNER=docker \
  uv run pytest -q tests/test_container_e2e.py
```

Use `container` instead of `docker` for the Apple-container E2E on a supported
Apple silicon Mac.

## Non-negotiable boundaries

- `plan` may inspect committed files but must not execute repository code.
- Detected commands run only inside a credential-free OCI container.
- Do not add host credential, socket, device, or broad filesystem mounts.
- Test-network isolation, immutable image identity, resource limits, and
  receipt/cache compatibility must remain machine-verifiable.
- Preserve Windows, macOS, Linux, Docker, Podman, and Apple `container`
  behavior relevant to the change.

Public receipt, plan, execution-policy, adapter, API, and exit-code changes
must update their version or compatibility gate, tests, schema where
applicable, README, and changelog.
