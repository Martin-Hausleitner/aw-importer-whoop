## Why
Home Assistant needs a minimal view of data fetched through the existing official WHOOP owner. A second OAuth client, another scheduler or a mixed ActivityWatch/CSV feed would break existing ownership and provenance.

## What Changes
- Add an opt-in private Home Assistant publisher after a complete successful official sync tick.
- Reuse the existing HA credential reference; persist only a bounded private projection and account hash.
- Preserve provider state and cadence when HA transport fails; retry only after the next regular tick.
- Validate private paths, file modes, exclusive publisher ownership, finite metrics and original timestamps.

## Capabilities
### New Capabilities
- `ha-official-feed`: bounded official-record projection and authenticated HA delivery without WHOOP token handoff.
### Modified Capabilities

## Impact
Only the isolated importer source, CLI, focused synthetic tests and documentation change. No live OAuth, ActivityWatch, HA or VM operation is performed by this change's tests.

## Acceptance
- Complete official ticks alone can publish; skipped AW records still contribute and incomplete provider ticks never advance the feed.
- Pending scores remain empty, units are explicit, all numbers are finite, naps do not replace main sleep and old amendments cannot displace newer periods.
- Private config/cache/credential references reject unsafe ownership, modes, links or replaced parents; account changes reject without exposing IDs.
- HA failures preserve the successful provider cursor and ordinary cadence; no extra WHOOP request, refresh, worker or timer is introduced.
- Source tests and OpenSpec validation pass; live acceptance remains separate.
