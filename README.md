<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-obsidian

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

> 🚧 **Planned.** Nothing to install yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What this plugin will do

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

It will declare every host, secret, path and intent it uses in `plugin.yaml`, and it will only return **intents**: the Horae core validates and applies them.

## Planned work

| Milestone | Epic |
|---|---|
| M3 Dogfood | **Obsidian vault plugin** — Read tasks from the vault and apply note and task intents to files the plugin owns. |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Development

The repository was scaffolded from [`template-plugin`](https://github.com/horae-dev/template-plugin):
an MCP stdio server built on [`horae-sdk`](https://github.com/horae-dev/horae/tree/main/sdk), a
`plugin.yaml` generated from the code, and CI (lockfile, ruff, mypy strict, pytest with an 85%
coverage gate, gitleaks, and the aggregate **CI OK** check). `horae-sdk` is not on PyPI yet:
`pyproject.toml` takes it from the core repository's `main` and `uv.lock` pins the commit.

```
uv sync
uv lock --check && uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov
uv run python -m horae_obsidian --manifest > plugin.yaml   # after changing tools or permissions
```

See [AGENTS.md](AGENTS.md) for every command.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
