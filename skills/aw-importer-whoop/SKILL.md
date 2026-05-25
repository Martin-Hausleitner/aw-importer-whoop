---
name: aw-importer-whoop
description: Operate and maintain the aw-importer-whoop repository, a WHOOP to ActivityWatch importer. Use when an agent needs to set up WHOOP OAuth, create or validate WHOOP Developer Dashboard credentials, log in through the local OAuth callback, request or import WHOOP export ZIPs, run API syncs, repair ActivityWatch WHOOP buckets, debug importer behavior, or change repository code/docs/tests.
---

# AW WHOOP Importer

## Overview

Use this skill to import the user's own WHOOP data into local ActivityWatch and to maintain the importer safely. The repository supports two data paths: WHOOP API OAuth sync for sleep/workout/cycle/recovery data, and WHOOP export ZIP backfills for CSV data including journal answers without private note text.

## First Checks

Run these before operating or editing:

```bash
git status --short --branch
python3 -m venv .venv
. .venv/bin/activate
pip install -e . pytest ruff
pytest -q
```

Use `ruff check .` before committing code changes. Check ActivityWatch with:

```bash
curl -fsS http://127.0.0.1:5600/api/0/info
```

## Privacy Guardrails

- Do not commit WHOOP exports, CSVs, OAuth tokens, ActivityWatch dumps, or personal health summaries.
- Do not print private signed export URLs unless the user explicitly asks.
- Keep `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`, access tokens, and refresh tokens out of logs and commits.
- Do not search email, open signed export links, or download a WHOOP export without explicit user approval for that run.
- Preserve the journal privacy behavior: import answer metadata and `has_notes`, but not journal note text.

## OAuth API Sync

Use this path for recurring WHOOP API data: sleep, workout, cycle, and recovery.

1. Open the WHOOP Developer Dashboard from `https://developer.whoop.com/`.
2. Create or use an app for this local importer.
3. Register this redirect URI exactly: `http://127.0.0.1:8765/callback`.
4. Request only the scopes this repo uses: `offline read:recovery read:cycles read:sleep read:workout`.
5. Do not request `read:profile` or `read:body_measurement` unless code has been updated to import that data and the user explicitly wants it.

Log in:

```bash
WHOOP_CLIENT_ID="..." WHOOP_CLIENT_SECRET="..." aw-importer-whoop login
```

The command starts a one-request local callback server, opens the WHOOP authorization URL, and saves tokens with private file permissions under the platform-specific user config directory. If browser opening is not useful, add `--no-open-browser`, copy the printed URL, sign in, authorize, and let WHOOP redirect back to the local callback.

To locate token/state files:

```bash
python3 -c 'from aw_importer_whoop.config import token_path,state_path; print(token_path()); print(state_path())'
```

Token files must be mode `0600`. Delete tokens only when intentionally logging out or switching WHOOP accounts.

Run the first sync as a one-shot:

```bash
WHOOP_CLIENT_ID="..." WHOOP_CLIENT_SECRET="..." aw-importer-whoop sync --once
```

API sync starts from the last saved cursor; on a fresh state it fetches only about the last 30 days. Use WHOOP export imports for older history.

Limit data types while debugging:

```bash
WHOOP_CLIENT_ID="..." WHOOP_CLIENT_SECRET="..." \
  aw-importer-whoop sync --once --type sleep --type recovery
```

For long-running services, use a private env file or keychain wrapper; do not put secrets in service `ExecStart` lines, shell history, or shared logs.

Common OAuth failures:

- Redirect mismatch: confirm the dashboard redirect URI is exactly `http://127.0.0.1:8765/callback`.
- No tokens found: run `aw-importer-whoop login` again.
- 401 after prior success: refresh token may have rotated or been revoked; log in again.
- 429: the client already retries with `Retry-After`; avoid tight manual loops.
- Concurrent syncs: run only one `sync` process at a time for a given user/config directory. Refresh tokens rotate, and concurrent syncs can invalidate each other or race on importer state.

## WHOOP Export ZIP Backfill

Use this path for historical CSV export imports. As of the current WHOOP support docs, personal data exports are requested in the WHOOP mobile app via More/App Settings/Data Export, and the export email can take up to 24 hours. Treat the emailed download link as a private signed URL.

Keep export archives outside the repo, for example `~/Downloads` or a private temp directory. Do not unzip into the repository. Inspect contents with `zipinfo -1 "$ZIP"` only if needed. After successful import and user approval, move the archive to Trash or another private archive location.

Expected files inside the ZIP:

- `sleeps.csv`
- `workouts.csv`
- `physiological_cycles.csv`
- `journal_entries.csv`

Always dry-run first. The importer accepts normal ZIPs and gzip-wrapped ZIP downloads:

```bash
aw-importer-whoop import-export ~/Downloads/my_whoop_data_YYYY_MM_DD.zip --dry-run
```

Import all supported CSVs:

```bash
aw-importer-whoop import-export ~/Downloads/my_whoop_data_YYYY_MM_DD.zip
```

Import selected data only:

```bash
aw-importer-whoop import-export ~/Downloads/my_whoop_data_YYYY_MM_DD.zip --type sleep --type journal
```

## ActivityWatch Verification

List WHOOP buckets after sync/import:

```bash
python3 - <<'PY'
import json, urllib.request
buckets = json.load(urllib.request.urlopen("http://127.0.0.1:5600/api/0/buckets/"))
for bucket_id, meta in sorted(buckets.items()):
    if bucket_id.startswith("aw-importer-whoop"):
        print(bucket_id, meta.get("type"), meta.get("created"))
PY
```

Expected buckets include:

- `aw-importer-whoop-sleep`
- `aw-importer-whoop-workout`
- `aw-importer-whoop-cycle`
- `aw-importer-whoop-recovery`
- `aw-importer-whoop-journal`

After import, verify event counts and journal privacy:

- Query each expected `aw-importer-whoop-*` bucket for the imported time range and confirm nonzero events when expected.
- For `aw-importer-whoop-journal`, inspect one event locally and confirm there is `has_notes` but no `Notes`, `notes`, or raw note text field.
- Do not paste event payloads into chat unless the user asks; summarize counts only.

## Repair Existing Events

Use repair when older WHOOP events exist but are missing flattened/queryable top-level fields:

```bash
aw-importer-whoop repair-existing --days 365 --dry-run
aw-importer-whoop repair-existing --days 365
```

Limit repair by bucket type when needed:

```bash
aw-importer-whoop repair-existing --type cycle --type recovery --days 365 --dry-run
```

## Development Notes

- API endpoints live in `src/aw_importer_whoop/whoop.py`.
- OAuth constants, scopes, token path, and ActivityWatch URL defaults live in `src/aw_importer_whoop/config.py`.
- Export CSV parsing lives in `src/aw_importer_whoop/export.py`.
- ActivityWatch event normalization and repair logic lives in `src/aw_importer_whoop/activitywatch.py`.
- Sync state uses stable hashes and stored ActivityWatch event ids. Preserve idempotency when changing imports.
- Add synthetic fixtures only. Do not add real WHOOP exports.
- Update `README.md`, `AGENTS.md`, and this skill when commands, scopes, bucket ids, or operational workflows change.

## Official References

- WHOOP Developer app setup: `https://developer.whoop.com/docs/developing/getting-started/`
- WHOOP OAuth docs: `https://developer.whoop.com/docs/developing/oauth`
- WHOOP API scopes/endpoints: `https://developer.whoop.com/api`
- WHOOP data export support article: `https://support.whoop.com/s/article/How-to-Export-Your-Data`
