## ADDED Requirements

### Requirement: Official complete-tick provenance
The importer SHALL publish only an opt-in projection created from records returned by its existing official WHOOP client during a complete successful tick. It MUST NOT read mixed ActivityWatch buckets, copy WHOOP credentials to HA or create a second OAuth owner or scheduler.

#### Scenario: A selective maintenance import runs
- **WHEN** the existing CLI selects fewer than all four official resource types
- **THEN** its normal import may complete
- **AND** the complete HA feed clock and bootstrap state remain unchanged.

#### Scenario: A provider tick fails partway
- **WHEN** a paged provider fetch or provider state save fails
- **THEN** no projection from that tick is committed or published
- **AND** its feed fetch time remains unchanged.

### Requirement: Private bounded publisher state
The publisher SHALL require owner-only regular configuration and credential files, an owner-only feed parent, an exclusive publisher lock and a bounded atomically fsynced cache. It MUST reject symlinks, hardlinks, replaced parents and account changes without logging payloads or identifiers.

#### Scenario: Another account or publisher appears
- **WHEN** a record belongs to another account or another publisher holds the private lock
- **THEN** the projection is not sent
- **AND** no credentials or account identifiers are printed or copied.

### Requirement: Honest minimal measurements
The projection SHALL contain only the agreed numeric metric allowlist and original resource timestamps. Missing and pending scores MUST NOT become zero. Non-finite numbers, invalid durations and naps replacing main sleep MUST be rejected or excluded as appropriate; resource period MUST outrank amendment time.

#### Scenario: A future resource or hidden ordering timestamp appears
- **WHEN** any measurement or ordering timestamp exceeds the completed tick time by more than sixty seconds
- **THEN** the staged feed is rejected before cache persistence or HTTP
- **AND** the future timestamp cannot displace a valid record during a later tick.

#### Scenario: An old period is amended later
- **WHEN** an older resource has a later updated time than a newer resource period
- **THEN** the newer resource remains selected
- **AND** the older amendment cannot relabel it as a fresh measurement.

### Requirement: Transport failures preserve provider behavior
HA transport SHALL use the existing credential-store origin without redirects or environment proxies and carry at most 64 KiB. Delivery failures MUST preserve successful importer state and cadence and retain only a minimal private pending projection for a later regular tick.

#### Scenario: An intermediary returns an HTTP-success page
- **WHEN** a 2xx response lacks a valid integration acknowledgement with accepted equal to true
- **THEN** the cached frame remains pending
- **AND** no response content is logged.

#### Scenario: HA rejects a completed official tick
- **WHEN** the authenticated HA POST fails
- **THEN** the provider tick still completes without a retry or refresh
- **AND** only a constant safe failure code is logged.
