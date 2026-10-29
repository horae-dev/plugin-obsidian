Closes #<issue>

## Summary

<!-- What changed and why, in a few lines. -->

## Acceptance criteria

<!-- Copy them from the issue; each maps to at least one test. -->
- [ ]

## Definition of done (see AGENTS.md)

- [ ] CI green: the **CI OK** check (lockfile, lint, types, tests + coverage, gitleaks)
- [ ] `uv run pre-commit run --all-files` passes locally
- [ ] `plugin.yaml` regenerated (`uv run python -m horae_obsidian --manifest > plugin.yaml`) if tools or permissions changed
- [ ] Fixtures in `tests/fixtures/` are synthetic (no personal data, no secret)
- [ ] Commits carry `Graft-Agent`, `Graft-Model` and `Graft-Task` trailers

## Declared additions

<!-- Write "none" or list each with a justification (they appear in plugin.yaml permissions). -->
- Network hosts:
- Secrets:
- Paths:
- Intents:
- LLM calls:
