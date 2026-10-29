<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-obsidian

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

> 🚧 **In progress.** The Tasks parser and the note sink are in; the Tasks source and the task sink are not, so there is nothing to install yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

## What this plugin will do

It will read the tasks of a vault as a Horae source and write notes and task status changes
back as sinks, touching only the files and blocks Horae owns. It will declare every host, secret, path and intent it uses in `plugin.yaml`, and it will only return **intents**: the Horae core validates and applies them.

## Reading Obsidian Tasks (#2)

`horae_obsidian.tasks.parse(data, path)` reads a note's bytes; `horae_obsidian.vault` reads
them from the vault (`read_note`, `scan`, the streaming `iter_scan`) without following any
symbolic link, at most 4 MiB a note, skipping hidden folders such as `.obsidian`. POSIX only;
a note ends in lower-case `.md` (`.MD` is ignored).

- **Tasks**: `- [ ]` lines with any list marker (`-` `*` `+` `1.` `1)`), nested, in
  blockquotes and callouts; not in frontmatter, fenced code or `%%` comment blocks. The
  checkbox rule is the Tasks plugin's: spaces (not tabs) after the marker, then `[c]` and
  the body, so `- [x]x` is a task. Statuses `[ ]` `[x]` `[X]` `[/]` `[-]`; any other
  character is kept as `custom` (whether a custom status is open or closed is #4's call).
- **Fields**, read from the end of the line as the Tasks plugin does, with or without U+FE0F:
  📅 📆 🗓 due, ⏳ ⌛ scheduled, 🛫 start, ➕ created, ✅ done, ❌ cancelled, 🔁 recurrence,
  🔺 ⏫ 🔼 🔽 ⏬ priority, 🆔 id, ⛔ depends on, 🏁 on completion, and the ⏰ reminder (Horae's
  `⏰ HH:MM` on the due date, or the Reminder plugin's `⏰ YYYY-MM-DD HH:MM`; `⏰ 9:00` and
  `⏰ 09:00:00` stay text). The set is the SDK's (`horae_sdk.tasks.SIGNIFIERS`); a test keeps
  them equal. A 🆔, ⛔ or `^block-id` value longer than 128 characters is text.
- **Tags** (`#tag`, not in inline code, `%%` comments, wikilinks, Markdown links, autolinks
  or bare URLs) and **hidden ids** (`🆔 id`, `^block-id`). The masking is Horae's own: the
  Tasks plugin reads tags on the raw text, so it may see a tag Horae does not (inside code or
  a link). Each `Task` also has its `heading` (the nearest one above) and its owned block
  (`<!-- horae:begin <name> -->` … `end`, as in the core's `horae.apply.notes`).
- **Not supported yet**: the Tasks global filter (every checkbox is a task) and Dataview
  inline fields (`[due:: 2026-10-22]` stays text).
- **Nothing is rewritten.** Every line keeps its exact text, line ending and byte span; BOM,
  CRLF and invalid UTF-8 are kept. `Document.render()` gives back the identical bytes, and
  `Document.replace_line(task, task.with_status("x"))` (or `task.tick(day)`,
  `task.untick()`) changes only that line. A value that does not parse (`📅 2026-02-30`) is
  kept as unknown, never interpreted. `Task.to_json()` / `Document.to_json()` are the
  JSON-safe projection (invalid UTF-8 as U+FFFD, flagged `invalid_utf8`).
- **Bounds.** Every scan of a line is linear in its length (1 MiB of hostile lines parses in
  well under a second). A line over 16 KiB, and every line after the first 100,000, is kept
  byte-exact but not analysed (`Document.warnings`: `long_line`, `too_many_lines`, and
  `unclosed_fence`, `unclosed_comment`, `unclosed_frontmatter`). A scan reads at most 10,000
  notes, 64 MiB and 200,000 tasks, then lists the rest as skipped (`truncated`); reported
  paths escape control and bidi characters.
- Vault text is untrusted (threat model T02): a title the SDK escaped
  (`horae_sdk.tasks.escape_title`) parses back with no field, tag or id of its own.

### Task identity

`Task.key` is the fingerprint #4's `task.update_status` intents will carry
(`horae_sdk.intents.fingerprint`, at most 128 characters of `A-Z a-z 0-9 . _ : -`):

| The task has | Key | Scope |
|---|---|---|
| a valid `🆔 id` | `obsidian:id:<id>` | the vault |
| else a `^block-id` | `obsidian:block:<path>:<block-id>` | the note |
| else | `obsidian:task:<path>:<digest>:<n>` | the note |

`<path>` is the note path in NFC (the raw path is kept for I/O). `<digest>` is 16 hex digits
of the SHA-256 of the description in NFC with runs of spaces and tabs as one space (other
whitespace, such as U+2028 or a no-break space, is text). `<n>` counts the earlier tasks of
the note with the same digest, every task counted whatever its key. If `fingerprint` refuses
a part, the key falls back to the digest form.

**Keeps the key**: changing the status, ticking or unticking (`tick` / `untick`), adding,
changing or removing dates, the reminder, the priority, recurrence, ⛔ or 🏁 fields; moving a
trailing tag among the fields; lines added or removed above (for 🆔 and block ids: anywhere,
and for 🆔: moving the task to another note).

**Changes the key**: editing the description (text or a tag inside it; trailing tags are
part of it), adding, changing or removing the 🆔 or the block id, renaming or moving the note
(digest and block keys), and, for digest keys, adding or removing an earlier task with the
same description in the note (its occurrence `<n>` moves).

**Collisions** (the same 🆔 in two notes, the same block id twice in a note, NFC-equal paths)
are flagged: `Task.key_unique` is false and the key is in `Document.duplicate_keys` /
`Scan.duplicate_keys`. #4's source will withhold such tasks and report them.

For compare-and-swap writes, `Task.line_sha256` hashes the line's exact bytes and
`Document.sha256`, `size`, `mtime_ns` and `ino` describe the note as read.

**Open questions for the maintainer (before #4)**

1. **A hidden Horae link.** Digest keys move when the description is edited; the options for a
   link that survives it are a block id `^h-<hash>`, a `🆔 h-<hash>` (also a Tasks
   dependency id), an inline `%% horae:<fingerprint> %%` comment, or state kept on the plugin
   side. Whether to write one, and the alias scheme for keys that change, is decided with
   #4.
2. **Completing a recurring task.** The Tasks plugin inserts the next occurrence above the
   ticked one; `Task.tick` refuses a recurring task (`RecurringTaskError`) until #4 decides
   the policy.
3. **⏰ and the Tasks plugin.** Tasks reads fields from the end of the line and may stop at a
   signifier it does not know, so in `📅 2026-10-22 ⏰ 19:00` (the order of
   `horae_sdk.tasks.task_line`) Tasks queries might not see the due date. To be verified in a
   real vault; if so, `task_line` should write `⏰` before the dates.

## Writing notes (#3)

The `obsidian.write_note` sink applies `note.create`, `note.append_section` and
`note.replace_owned_block`. **The core computes every note's bytes** (`horae.apply.notes`,
the same conventions as its dry-run diff) and calls the sink operations `obsidian.read`,
`.apply`, `.restore`, `.parent_exists` and `.identity`; the plugin writes exactly those bytes
(`horae_obsidian.writer`) and checks them again on its side:

- **Owned files** are notes whose name ends in `.assistant.md` (the core's rule). Only those
  are created (never over an existing note: `note_exists`), appended to (the note must exist
  and keep its bytes) or removed by `horae undo`.
- **Owned blocks** are `<!-- horae:begin <block> -->` … `<!-- horae:end <block> -->`. In any
  other note only the lines of one well-formed block already there may change: duplicated,
  nested, unbalanced or malformed markers are `block_ambiguous` (the core's rules: a marker
  inside a code fence counts), a change anywhere else is `not_owned`.
- **The vault** is the single `paths.write` entry granted in `horae.lock` (the manifest asks
  for none). Note paths end in `.md` with no hidden segment (never `.obsidian/`), no `..`,
  `\`, `:` or control character; a symbolic link anywhere below the vault is never followed;
  a name that exists in another case or Unicode form (`Today.md` / `today.md`, NFC / NFD) is a
  `path_collision`, since a synced vault reaches case-insensitive devices.
- **Writes** are compare-and-swap on the SHA-256 the core planned (`target_changed`), then a
  temporary file in the note's directory, `fsync`ed, renamed over the note and the directory
  `fsync`ed, the note's permission bits kept; a failure leaves the note as it was. As for the
  core's own `file-sink`, an editor or sync client writing the same note at that very moment
  can still lose its write.

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
