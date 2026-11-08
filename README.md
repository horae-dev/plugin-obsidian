<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-obsidian

Horae plugin: read and write an Obsidian vault (Obsidian Tasks syntax), owning only its own files and blocks.

> 🚧 **In progress.** The Tasks parser, the note sink, the `obsidian.open_tasks` source, the task sink and the `obsidian.reminders` transform are in; nothing is released yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

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

Horae's Tasks writer ends every task line it creates with the block id
`^h-<12 hex of SHA-256(fingerprint)>` (horae-sdk `task_block_id`) and keeps a
`hash → fingerprint` index; `⏰` goes before the Tasks dates (so the real Tasks plugin, which
reads fields from the end and stops at a signifier it does not know, still sees `📅`; both
orders are read). The lines are horae-sdk 0.3's `task_line(..., fingerprint=)`.

**`obsidian.open_tasks`** (source; parameters `limit`, default 500, at most 2000, and
`open_statuses`, default `" /"`) scans the vault (budgets above) and returns the open tasks
by due date: `key`, `source_fingerprint`, description, status, note path and line,
`line_sha256`, heading, due / scheduled / start, reminder, `snoozed_until` / `snoozed`, tags,
the `[link](…)` (Horae's tasks only), priority, recurrence, 🆔, block id and `edited`,
JSON-safe. Alongside: `withheld` (a key that is a duplicate, `duplicate_key`, or not known
unique, `uniqueness_unknown`), `hidden` (Horae's output, with note, line and reason; counted in
`horae_output`), `edited_horae_tasks` (Horae's tasks edited in Obsidian, still listed),
`not_open_statuses` (theme statuses not counted as open), `skipped`, `truncated`, `complete`,
`index`, and `more` / `output_cut` (at most 4 MiB of JSON). None of these fails the run. The
source writes nothing.

**`obsidian.write_task`** (sink, an `act` entry with `mode: tasks`; parameters `note`,
default `Tasks.assistant.md`, and `recreate_after_days`, default 30; `link_prefix` and other
routing parameters are accepted and not checked again: the core checked them) applies:

- `task.update_status`. `task` is a key from `open_tasks`, or the fingerprint of the
  `task.create` that wrote the task. A fingerprint (or an `obsidian:horae:` key) is looked
  up in its writer note, read alone; otherwise the vault is scanned. Refused, changing
  nothing: no such task (`task_not_found`), several, or one not shown unique
  (`task_ambiguous`), Horae's output, snoozing a closed task (`task_closed`), and a note
  changed under it (`target_changed`). Once located, the note is read again, the task found
  again by key and line digest, and that note's fresh SHA-256 is the compare-and-swap value
  (tried once more on `target_changed`). **Only that line changes**: `done` sets `[x]` and
  `✅ YYYY-MM-DD` (today in the instance time zone, the `TZ` the core sets), `cancelled` `[-]`
  and `❌ YYYY-MM-DD`, `todo` reopens a closed task (`[ ]`, both dates removed) and leaves an
  open one as it is, `snoozed` writes the core's `until` as Horae's reminder before the Tasks
  dates (`⏰ YYYY-MM-DD`, or `⏰ YYYY-MM-DD HH:MM` in the instance time zone by its rules at
  that date, rounded up to the minute). A task already in that state, and a recurring task
  asked `done` or `cancelled`, is left alone (`changed: false`; `recurring: "tick in
  Obsidian"`). The details record the note, the line and both line digests.
- `task.create`. The line is appended to the entry's `note` (an owned note only, `not_owned`),
  created when missing, under the index lock, then the index entry is written. It is not
  written again while the fingerprint's task is in the vault (in its writer note, edited or
  not, or moved or copied to another note); an unindexed line with its block id in the writer
  note is adopted (`adopted`); a task the user deleted is `dismissed` for
  `recreate_after_days`. When the scan did not read every note, it is still created, with
  `dedupe_complete: false` and a `dedupe_warning`.

**The index** is `.horae/obsidian-tasks.json` in the vault (the hidden folder the scan skips),
mode 0700, written atomically under an exclusive `flock` (`index_busy` after 8 s). An entry is
used only when its block id is the hash of its fingerprint. Corrupt content starts over;
another `format`, a symbolic link or an unreadable file is `index_unavailable` for the sink,
and the source reports it (`index`) without failing.

### Decisions

Recorded from the review of #4 (maintainer: "go with what's good for the project").

1. **Losing the index** (deleted, corrupt, restored from an old backup) hides the Horae tasks
   of the writer notes (`hidden`, reason `unverified_line`) and makes `task.update_status` by
   fingerprint `task_not_found`, until the next `task.create` of that fingerprint: an
   unverified `^h-<block>` line for that exact block in the writer's note is **adopted** (the
   core's intent proves fingerprint → hash), its index entry rewritten, `changed: false`.
   Several such lines: `task_unadoptable` (keep one). Nothing is written twice.
2. **Writer notes are writer-exclusive.** `Tasks.assistant.md` and every note the index
   records a task (or a tombstone) in are refused to note intents and their undo
   (`tasks_note`). Inside a writer note, a line whose `^h-` block id the index maps to that
   note is Horae's task, edited in Obsidian or not (no line-digest requirement): ticking or
   re-dating it there no longer hides it. Other owned notes are LLM output, never read back.
   `open_tasks` lists hidden lines (`hidden`) and edited Horae tasks (`edited_horae_tasks`)
   with note and line, so nothing disappears silently. A **copy** of a Horae line in a user
   note (same block id) while the writer note holds it is a plain user task keyed by its own
   note (`obsidian:block:<note>:h-…`): not ambiguous, not found by the fingerprint. A line
   **moved** out of the writer note keeps the link.
3. **Time zone rules**: date-times are converted with the zone `TZ` names
   (`ZoneInfo`), else the process's local rules, never the offset of the current moment
   (a July snooze written in November is at summer time).
4. **`snoozed` on a closed task** (done, cancelled) is refused (`task_closed`); reopening stays
   an explicit `todo`.
5. **`todo` on an open task** (any open status, `[/]` and a theme's statuses included) is a
   no-op: its status is kept.
6. **One note read**: after locating, only the target note is read again for the write
   (fresh SHA-256 as the compare-and-swap value, one retry on `target_changed`); a fingerprint
   is found through the index path without a scan. On 5,000 notes: 0.64 s → 1 ms by
   fingerprint; by key, still a scan (0.65 s), with the race window down to one note read.
7. **Trust limit.** The index is checked (the block id must be the hash of its fingerprint),
   not signed: anyone with write access to the vault, `.horae/` included (through sync), can
   make a line Horae's output or Horae's task. The vault-write boundary is the trust limit.
   The index stays in the vault: it must travel with the vault for restores. The block id has
   48 bits: two fingerprints colliding is about 2e-5 at 100,000 tasks, and the index refuses
   a second fingerprint on a known block id (`block_collision`); a second preimage (a chosen
   fingerprint hitting a given block id) costs about 2^48 hashes, and only lets a vault
   writer do what vault write access already allows.
8. **Pruning**: an index entry whose line still exists is never dropped; past 20,000 entries,
   entries whose line was not seen are dropped, oldest first.
9. **Relinking**: after a status change the index records the line's new digest, tried twice;
   when it still fails (`index: "stale"`), the line is only reported as `edited` (ownership
   does not depend on the digest) and the next change re-records it.
10. **No line text in `Applied.details`** (digests only) until the core wires task undo;
    `restore_status` takes the replaced line from the caller.
11. **Tombstones**: when an indexed task line is gone from the vault (seen by a complete scan
    during a `task.create`), it becomes a tombstone; `task.create` of that fingerprint is
    `dismissed` for `recreate_after_days` (default 30), then written again.
12. **Bounded output**: at most 4 MiB of JSON (`output_cut`, `more`).
13. **Links**: `link` is reported for Horae's tasks only (a hand-written `[link](…)` is
    text); the SDK's `task_line` renders it (`link_destination`); `link_prefix` is not checked
    again. `plugin.yaml` keeps `horae: >=0,<0.2`, like the other plugins.
14. **Snoozes**: `snoozed_until` is a `⏰` date or date-time still in the future, `snoozed`
    its boolean; consumers decide (the reminders pack skips snoozed tasks).
15. **Recurring tasks**: `done` / `cancelled` change nothing and say `recurring: "tick in
    Obsidian"` (Horae never ends a recurrence) instead of failing the run.
16. **Custom statuses**: open means `[ ]` and `[/]` unless `open_statuses` lists more; the
    others are counted in `not_open_statuses`.
17. **Withheld and hidden** tasks are reported (`withheld`, `hidden`, counts), never a failed
    run.

**Limits.** A note whose path holds `:` or `\` is read but not written (`path_invalid`). A
`task.update_status` by key scans the whole vault.

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

## Reminders: the `obsidian.reminders` transform (#9)

A deterministic transform for [pack-reminders](https://github.com/horae-dev/pack-reminders)'
`remind` pipeline: it turns the collected `obsidian.open_tasks` data into `notify.push`
intents, with no LLM, no clock (the pipeline passes `{{now}}` and `{{timezone}}`), no
network, secret or path. `notify.push` is in `permissions.intents` because the transform
proposes it; a notifier (plugin-ntfy) applies it. The rules are in the module docstring of
[`reminders.py`](src/horae_obsidian/reminders.py).

| Parameter | Meaning |
|---|---|
| `now` | Required. The run's start, RFC 3339 with an offset: `"{{now}}"` |
| `timezone` | Required. IANA zone of the wall-clock times: `"{{timezone}}"` |
| `window_minutes` | 1 to 720, default 60: the lookback. Every run proposes what falls in `(now − window, now]`, so a missed run is caught up; the notifier's daily dedupe drops the repeats. Must be at least the cron interval (the operator's choice) |
| `digest_at` | `"HH:MM"`; omitted: no digest. Proposed by every run whose lookback holds it (today's or yesterday's, across midnight) |
| `overdue_at` | `"HH:MM"`; omitted: no overdue nudge |
| `overdue_for_minutes` | 1 to 1440, default 60: how long nudges are proposed from `overdue_at` |
| `max_overdue` | 0 to 100, default 10: nudges per run, and overdue lines in the digest |
| `quiet_hours` | `{start: "HH:MM", end: "HH:MM", during: delay\|drop}`, the notifier's. `delay` (default): a task whose reminder fell in them before the digest is left out of it (its own delayed push arrives then). `drop`: the notifier would drop that push, so none is proposed and the digest lists the task under `During quiet hours:` |

An unknown or invalid parameter fails the call; a malformed task is skipped and counted.

- **`⏰` reminders**: `⏰ 2026-11-09 14:30` / `Call the plumber`. The date is in the title, so a
  daily recurring reminder is a new push each day. A bare `⏰ HH:MM` is that time on the due
  date (no due date: no reminder). DST: a skipped time fires at the first instant after the
  gap, a repeated one at its first occurrence (the core scheduler's rule).
- **Snoozed** is recomputed from the `⏰` and `now`; the source's `snoozed` is only a hint.
- **The digest** (`Today, 2026-11-09`): `Overdue: N` and the oldest `max_overdue`, then
  `Today: N`, by reminder time, then the rest due that day. At most 3,800 UTF-8 bytes, never
  cut mid-line; `Some tasks could not be read` when the source left tasks out (`more`,
  `output_cut`, `withheld`, `hidden`, `skipped`).
- **Overdue nudges** (`Overdue: due 2026-11-08`): only the tasks that became overdue (due the
  day before); older ones are in the digest only.
- **Text**: Markdown links become their text, `#tags` are dropped, the description is one
  line of at most 200 characters, cut on a word boundary. Two pushes of one run with the same
  text get a short stable suffix from their task keys (` · 3fa2`).

**Golden vectors** for consumers ship in the package
(`horae_obsidian.vectors.reminder_vectors()`, `src/horae_obsidian/vectors/reminders.json`):
pack-reminders runs them too. Regenerate after a deliberate change of behaviour with
`uv run python tests/data/reminder_vectors.py > src/horae_obsidian/vectors/reminders.json`
and review the diff.

## Daily note: the `obsidian.render_note` transform (#5)

A deterministic transform that lays out the assistant note: it takes the collected data
(`obsidian.open_tasks`, a `calendar.*` source, a `github.*` source) and proposes **one**
`note.replace_owned_block`, with no LLM, clock, network, secret or path. The daily brief's
LLM keeps wording the summary and the ranking (`summary`, `ranked` below); this transform
makes the structure, so the core's note-text guard (decision 55) never has to judge a
checkbox, wikilink, `#tag` or `::`. Rules: [`render.py`](src/horae_obsidian/render.py).

| Parameter | Meaning |
|---|---|
| `today` | Required. `YYYY-MM-DD`: `"{{date}}"`. The only notion of time (no clock is read) |
| `block` | Owned block id, default `assistant-daily` |
| `path` | Note path; omitted, the `act` entry's |
| `template` | Override of the default template (below) |
| `timezone` | IANA zone to show event times in (`"{{timezone}}"`); omitted: as the source wrote them |
| `summary`, `ranked` | The LLM's text and `[{title, why}]`, escaped like everything else |
| `expect` | Source refs that must have given data (`[calendar.events]`): a missing one is an alert |
| `max_items` | 1 to 100, default 20 lines per section or task group, then `…and N more` |

Tasks are grouped by due date (Overdue, Today, Tomorrow, Later, No date) and sorted, so the
same input gives the same bytes, in any input order. An example (`today: 2026-11-09`,
`timezone: Europe/Paris`):

````markdown
# Assistant, 2026-11-09

## Alerts

_None._

## Focus

_None._

## Calendar

- 09:30 Standup
- 12:00 Lunch
- 2026-11-10 Offsite

## Tasks

### Overdue (1)

- Pay rent (due 2026-11-01)

### Today (1)

- Write report

### Tomorrow (1)

- Call mum

### Later (1)

- Plan trip (due 2026-12-01)

### No date (1)

- Someday

## GitHub

- acme/app 42 Fix the thing
````

**Untrusted text is inert.** Every task description, event title, pull request title and LLM
string goes through the SDK's `escape_title` and is written as a plain bullet, never as a
checkbox: it cannot become a live task, link, tag, field or signifier, and a later
`obsidian.open_tasks` read finds no task in the note (the tests check this with this plugin's
own parser).

**Overriding the template, per instance.** Set the `template` parameter in the instance's
pipeline. It is not read from the vault, because a vault note is not trusted configuration and
the plugin then needs no extra path. The format is logic-less: literal text, `${name}`
placeholders (`date`, `summary`, `focus`, `alerts`, `calendar`, `tasks`, `github`) and `$$`
for a dollar sign; no loops, conditions or includes (`{{ }}` is the core's own parameter
syntax). Each placeholder is replaced by its section's body, `_None._` when empty, so the
headings are yours:

```yaml
- obsidian.render_note:
    today: "{{date}}"
    template: |
      # Day ${date}

      ${alerts}

      ${tasks}
```

A template is refused with an error naming the reason when it has an unknown placeholder, a
stray `$`, more than 4,000 characters, or what the guard refuses in note text (a checkbox
line, wikilink, `#tag`, `::`, `%%`, a Tasks signifier, an owned-block marker, a code fence,
`<%`, frontmatter, a control character). The rendered note is at most 64 KiB.

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
