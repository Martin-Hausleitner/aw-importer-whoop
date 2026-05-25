# Agent Guide

This repository imports the user's own WHOOP data into local ActivityWatch.

## Start Here

- Use the repo-local skill at `skills/aw-importer-whoop/SKILL.md` for operating, debugging, or changing this project.
- Read `README.md` for the human-facing overview and CLI examples.
- Check `git status --short --branch` before editing. Do not overwrite unrelated local changes.

## Privacy Rules

- Never commit WHOOP export ZIPs, CSVs, OAuth tokens, ActivityWatch dumps, or personal health summaries.
- Treat WHOOP export email links as private signed URLs.
- Keep `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`, access tokens, and refresh tokens out of logs and commits.
- Journal note text is intentionally not imported into ActivityWatch. Preserve that behavior.

## Development Loop

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e . pytest ruff
pytest -q
ruff check .
```

ActivityWatch is expected at `http://127.0.0.1:5600/api/0`.

## Operational Checklist

- For API sync, create/use a WHOOP developer app, register `http://127.0.0.1:8765/callback`, run `aw-importer-whoop login`, then run `aw-importer-whoop sync --once` first.
- For export backfills, request the WHOOP data export, download the ZIP privately, run `import-export --dry-run`, then import.
- After imports, verify `aw-importer-whoop-*` buckets in ActivityWatch.

## When Changing Code

- Keep imports idempotent and state-backed.
- Keep ActivityWatch event data queryable with useful top-level fields plus the normalized record.
- Add or update tests for parser mappings, state behavior, privacy filtering, and repair/sync behavior.
- Update `README.md`, this file, and the repo-local skill whenever workflows or commands change.
