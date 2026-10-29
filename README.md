<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-obsidian

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

> 🚧 **In progress.** The Tasks parser is in; the source and sinks are not, so there is nothing to install yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What this plugin will do

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

It will declare every host, secret, path and intent it uses in `plugin.yaml`, and it will only return **intents**: the Horae core validates and applies them.

## Reading Obsidian Tasks (#2)

`horae_obsidian.tasks.parse(data, path)` reads a note's bytes; `horae_obsidian.vault` reads
them from the vault (`read_note`, `scan`) without following any symbolic link, at most 4 MiB a
note, skipping hidden folders such as `.obsidian`.

- **Tasks**: `- [ ]` lines with any list marker (`-` `*` `+` `1.` `1)`), nested, in
  blockquotes and callouts; not in frontmatter, fenced code or `%%` comment blocks. Statuses
  `[ ]` `[x]` `[X]` `[/]` `[-]`, any other character kept as `custom`.
- **Fields**, read from the end of the line as the Tasks plugin does, with or without U+FE0F:
  📅 📆 🗓 due, ⏳ ⌛ scheduled, 🛫 start, ➕ created, ✅ done, ❌ cancelled, 🔁 recurrence,
  🔺 ⏫ 🔼 🔽 ⏬ priority, 🆔 id, ⛔ depends on, 🏁 on completion, and the ⏰ reminder (Horae's
  `⏰ HH:MM` on the due date, or the Reminder plugin's `⏰ YYYY-MM-DD HH:MM`). The set is the
  SDK's (`horae_sdk.tasks.SIGNIFIERS`); a test keeps them equal.
- **Tags** (`#tag`, not in code, links, URLs or comments) and **hidden ids** (`🆔 id`,
  `^block-id`). `Task.key` is a stable fingerprint: the 🆔 id, else the block id, else a
  digest of the note path and the description (ticking a task or changing its dates keeps it).
- **Nothing is rewritten.** Every line keeps its exact text, line ending and byte span; BOM,
  CRLF and invalid UTF-8 are kept. `Document.render()` gives back the identical bytes, and
  `Document.replace_line(task, task.with_status("x"))` changes only that line. A value that
  does not parse (`📅 2026-02-30`) is kept as unknown, never interpreted.
- Vault text is untrusted (threat model T02): a title the SDK escaped
  (`horae_sdk.tasks.escape_title`) parses back with no field, tag or id of its own.

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
