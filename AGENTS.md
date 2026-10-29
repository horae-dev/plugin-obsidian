# AGENTS.md — how to work in `plugin-obsidian`

This repository is built mostly by **AI coding agents** (Claude Code and others) supervised by a human maintainer. These rules apply to agents and humans alike. `CLAUDE.md` imports this file.

## Before you start
1. Work on **one issue at a time**. Only pick issues with the `agent-ready` label and executor `Agent` or `Agent + human check`.
   - `Human-led`: open a **draft PR with a short design note first** and wait for the maintainer before writing the implementation.
   - `Human-only`: do not pick it.
2. Read the issue's **acceptance criteria**, the linked epic, and the relevant section of the [plan](https://github.com/horae-dev/horae/blob/main/docs/PLAN.md).
3. If anything is ambiguous, **ask in the issue** instead of guessing. Unclear specs are the top cause of wasted agent work.

## Setup
```
# install uv: https://docs.astral.sh/uv/getting-started/installation/
uv sync
uv run pre-commit install   # optional: ruff and gitleaks on every commit
```

## Commands
```
uv lock --check                   # uv.lock matches pyproject.toml
uv run ruff check                 # lint
uv run ruff format --check        # formatting (drop --check to fix)
uv run mypy                       # types (strict)
uv run pytest --cov               # tests: recordings replayed, manifest checked, stdio served; coverage gate 85%
HORAE_SDK_RECORD=1 uv run pytest  # rewrite tests/recordings from the current outputs (review the diff; refused in CI)
uv run python -m horae_obsidian --manifest > plugin.yaml   # regenerate the manifest
uv run pre-commit run --all-files # ruff + gitleaks
```
CI runs the same checks; the aggregate **CI OK** check must be green before merge.

## Working rules
- Branch: `story/<issue-number>-<short-slug>`. One PR per issue; keep it under ~400 changed lines when possible.
- Python 3.12+, managed with **uv**. Style: ruff, mypy strict, pytest. See [Commands](#commands).
- Plugin rules ([horae-sdk README](https://github.com/horae-dev/horae/blob/main/sdk/README.md)): never `print` (stdout is the protocol channel); untrusted fields reach a task title only through `intents.task_create`; regenerate `plugin.yaml` when tools or permissions change; recordings and fixtures are synthetic.
- **Tests first** for behaviour: every acceptance criterion maps to at least one test.
- Never add a network host, secret, path, intent or LLM call that is not declared and justified in the PR.
- Never commit secrets, tokens, personal data or real hostnames. Fixtures must be synthetic.
- Do not change `graft.yaml`, `AGENTS.md` or CI permissions unless the issue asks for it.

## Commits and provenance (Graft)
Every commit made by an agent carries Graft trailers in its last paragraph:

```
Graft-Agent: claude-code
Graft-Model: <model id>
Graft-Task: #<issue number>
```

Add the trailers yourself: `git commit --trailer "Graft-Agent: claude-code" --trailer "Graft-Model: <model id>" --trailer "Graft-Task: #<issue number>"`. For work tracked in another repository's issue, write it as `Graft-Task: <owner>/<repo>#<issue number>`.

## Review
- Graft posts an intent card and applies [`graft.yaml`](graft.yaml).
- Trivial and small low-risk changes can be approved without a human.
- Changes touching sensitive paths (`plugin.yaml` (permissions), anything under `auth/`, workflows, `pyproject.toml` (dependencies), `uv.lock` (the pinned dependencies, including the horae-sdk commit), `.pre-commit-config.yaml`, `renovate.json`), large changes and anything Graft rates `sensitive` need the maintainer.
- Address review comments in new commits; don't force-push over reviewed commits.

## Definition of done
- [ ] All acceptance criteria met and checked in the PR description
- [ ] CI green: the **CI OK** check (lockfile, lint, types, tests + coverage, gitleaks)
- [ ] Docs and examples updated if behaviour changed
- [ ] No new undeclared permissions, secrets or LLM calls
- [ ] On the issue, record **actuals**: agent attempts, wall-clock time, whether a human took over. These feed the [estimation](https://github.com/horae-dev/horae/blob/main/docs/ESTIMATION.md) recalibration.
