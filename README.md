<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-obsidian

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

> 🚧 **In progress.** The Tasks parser, the note sink, the `obsidian.open_tasks` source and the task sink are in; nothing is released yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

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
  character is kept as `custom`, which counts as **open** (the Tasks plugin treats a status
  it does not know as a to-do).
- **Fields**, read from the end of the line as the Tasks plugin does, with or without U+FE0F:
  📅 📆 🗓 due, ⏳ ⌛ scheduled, 🛫 start, ➕ created, ✅ done, ❌ cancelled, 🔁 recurrence,
  🔺 ⏫ 🔼 🔽 ⏬ priority, 🆔 id, ⛔ depends on, 🏁 on completion, and the ⏰ reminder (Horae's
  `⏰ HH:MM` on the due date, or the Reminder plugin's `⏰ YYYY-MM-DD HH:MM`; `⏰ 9:00` and
  `⏰ 09:00:00` stay text), read **before** the Tasks dates (Horae's order since horae-sdk
  0.3) as well as after them (0.1 and 0.2). The set is the SDK's
  (`horae_sdk.tasks.SIGNIFIERS`); a test keeps
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
| else Horae's block id `^h-<12 hex>` | `obsidian:horae:h-<12 hex>` | the vault |
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
and for 🆔 and `^h-`: editing the text, moving the task to another note).

**Changes the key**: editing the description (text or a tag inside it; trailing tags are
part of it), adding, changing or removing the 🆔 or the block id, renaming or moving the note
(digest and block keys), and, for digest keys, adding or removing an earlier task with the
same description in the note (its occurrence `<n>` moves).

**Collisions** (the same 🆔 or `^h-` in two notes, the same block id twice in a note,
NFC-equal paths) are flagged: `Task.key_unique` is false and the key is in
`Document.duplicate_keys` / `Scan.duplicate_keys`. Those only cover the notes read:
`Scan.keys_complete` is false when the scan was truncated or skipped any note, and
`Scan.known_unique(task)` fails closed (a vault-scoped key is only known unique after a
complete scan; a note-scoped key, whose note was read whole, unless the scan was truncated or
skipped a path equal to it in NFC). A result of `iter_scan` is **never** vault-unique.

For compare-and-swap writes, `Task.line_sha256` hashes the line's exact bytes and
`Document.sha256`, `size`, `mtime_ns` and `ino` describe the note as read.

## Open tasks and task status (#4)

Decisions (maintainer, on #4): Horae's Tasks writer ends every task line it creates with the
block id `^h-<12 hex of SHA-256(fingerprint)>` (horae-sdk `task_block_id`) and keeps a
`hash → fingerprint` index; `⏰` goes before the Tasks dates (so the real Tasks plugin, which
reads fields from the end and stops at a signifier it does not know, still sees `📅`); Horae
never closes a recurring task; ambiguous keys are withheld and refused.

**`obsidian.open_tasks`** (source, parameter `limit`, default 500, at most 2000) scans the
vault (budgets above) and returns the open tasks (`todo`, `in_progress`, `custom`) by due date:
`key`, `source_fingerprint`, description, status, note path and line, `line_sha256`, heading,
due / scheduled / start, reminder, tags, the `[link](…)`, priority, recurrence, 🆔 and block
id, JSON-safe. A task whose key is a duplicate (`duplicate_key`) or not known unique
(`uniqueness_unknown`) is listed in `withheld`, not in `tasks`; `skipped`, `truncated` and
`complete` describe the scan. **Horae's own output is not read back**: a line in an owned
block, or in an owned note (`*.assistant.md`), is skipped (counted in `horae_output`) unless
it ends with a `^h-` block id the index maps, i.e. Horae's Tasks writer wrote it for a
`task.create`; such tasks carry their `source_fingerprint` (e.g. `github:pr:42`), so a
consumer can tell them from the user's tasks and dedupe them against the live source.
Otherwise an LLM-written note, or a prompt injection it obeyed, would come back the next day
as the user's own task. The source writes nothing.

**`obsidian.write_task`** (sink, an `act` entry with `mode: tasks`; `link_prefix` and other
routing parameters are accepted and ignored) applies:

- `task.update_status`. `task` is a key from `open_tasks`, or the fingerprint of the
  `task.create` that wrote the task (found by its `^h-` block id anywhere in the vault). The
  sink scans the vault and refuses, changing nothing: no such task (`task_not_found`), several,
  or one not shown unique (`task_ambiguous`), Horae's own output, closing a recurring task
  (`recurring_task`: `done` or `cancelled`; tick it in Obsidian, where Tasks creates the next
  occurrence; `snoozed` and `todo` are fine), and a note changed since the scan
  (`target_changed`: compare-and-swap on `Document.sha256`, and the line's `line_sha256`
  checked again in the bytes re-read before the rename). **Only that line changes**:
  `done` sets `[x]` and `✅ YYYY-MM-DD` (today in the instance time zone, the `TZ` the core
  sets), `cancelled` `[-]` and `❌ YYYY-MM-DD`, `todo` `[ ]` without either date, `snoozed`
  writes the core's `until` as Horae's reminder before the Tasks dates (`⏰ YYYY-MM-DD` for a
  date, `⏰ YYYY-MM-DD HH:MM` for a date-time in the instance time zone, rounded up to the
  minute) and reopens a closed task. A task already in that state is left alone
  (`changed: false`). The details record the note, the line and both digests;
  `tasksink.restore_status(paths, details)` puts the exact line back (the core does not undo
  task changes yet: a follow-up wires it to `horae undo`).
- `task.create`. The line is horae-sdk's `task_line` (title escaped, link rendered inert with
  `link_destination`), then `⏰ HH:MM`, `📅 YYYY-MM-DD` and `^h-<hash>`, appended to the
  entry's `note` parameter (default `Tasks.assistant.md`; an owned note only, `not_owned`),
  created when missing. A fingerprint whose block id is already in the vault is not written
  again (`changed: false`). The index entry is written first.

**The index** is `.horae/obsidian-tasks.json` in the vault (the hidden folder the scan skips),
mode 0700, written atomically under an exclusive `flock` (`index_busy` after 8 s), at most
20,000 entries. An entry is used only when its block id is the hash of its fingerprint, so an
edited index can drop a link, never misdirect one. Corrupt content starts over; another
`format`, a symbolic link or an unreadable file is `index_unavailable` for the sink, and the
source reports it (`index`) without failing.

**Limits.** A note whose path holds `:` or `\` is read but not written (`path_invalid`). A
second `^h-` copy of a task (a copied line) makes both ambiguous: remove one. The sink scans
the whole vault for each intent.

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
  other note, `note.replace_owned_block` may only change the lines of its one well-formed
  block already there: duplicated, nested, unbalanced or malformed markers are
  `block_ambiguous` (the core's rules: any case counts, and so does a marker inside a code
  fence), a change anywhere else is `not_owned`. In an owned note the block must end up there
  exactly once and the rest of the note is kept.
- **Markers on line 1 of a note with a BOM.** As in the core, the BOM is part of the first
  line, so a marker there is not canonical: the core refuses the block (`block_ambiguous`)
  and so does the writer. Put the block below the first line. (The Tasks parser reads after
  the BOM and does see such a block as owned; it never writes.)
- **`horae undo`** puts every changed note back. In a note Horae does not own it may change
  any number of blocks (each one well-formed marker pair before and after), but nothing
  outside them; a malformed or prose marker elsewhere does not stop it. If you edited such a
  note **outside** its blocks after the run, the undo is refused (`not_owned`), even with
  `horae undo --force`, which would rewrite the whole note: **repair it by hand** (the
  journal keeps the original bytes), or move your edit, undo, then put it back.
- **The vault** is the single `paths.write` entry granted in `horae.lock`, an absolute path
  (the manifest asks for none). Note paths end in `.md` with no hidden segment (never
  `.obsidian/`), no `..`, `\`, `:`, control character or lone surrogate, at most 255 UTF-8
  bytes a segment; a symbolic link anywhere below the vault is never followed.
- **`path_collision`.** A name that exists in another case or Unicode form (`Today.md` /
  `today.md`, NFC / NFD) is refused, since a synced vault reaches case-insensitive devices.
  The error names the file on disk: rename it on disk, or make the pipeline use that exact
  name.
- **Writes** are compare-and-swap on the SHA-256 the core planned (`target_changed`), then a
  temporary file in the note's directory, `fsync`ed, then linked as a new note (never over
  one created meanwhile) or renamed over the note once its bytes are re-read and hashed, and
  the directory `fsync`ed, the note's permission bits kept; a failure leaves the note as it
  was. As for the core's own `file-sink`, an editor or sync client writing the same note at
  that very moment can still lose its write.

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

The writer repeats the core's block-marker rules. `tests/data/core_note_vectors.json` holds
what the core's real `note_after` makes of a set of notes (CRLF, BOM, markers at the start
and end, empty, indented and upper-case markers, several blocks), with the core commit in its
header; the tests check the writer accepts all of it. After a change of
`horae.apply.notes`, regenerate it from a core checkout:
`uv run python tests/data/core_vectors.py ../horae origin/main > tests/data/core_note_vectors.json`.

See [AGENTS.md](AGENTS.md) for every command.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
