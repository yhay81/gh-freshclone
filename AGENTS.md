# AGENTS.md

## Project contract

`gh-freshclone` proves that an immutable GitHub commit passes a conservative
baseline in a credential-free container.

- Keep `plan` free of repository code execution.
- Never run detected commands directly on the host.
- Never forward host credentials or broad host mounts to a runner.
- Inspect only committed files for local repositories.
- Keep detection deterministic and evidence-backed; do not execute README text
  or arbitrary CI shell blocks.
- Preserve Windows, macOS, and Linux behavior. On macOS, support Apple
  `container` as well as Docker/Podman fallback.
- Treat receipt schema and exit codes as public interfaces.

After changes, run:

```shell
uv run pytest -q
uv run ruff check .
```

Do not commit or push unless the user asks.
