## 1. Contract and implementation
- [x] 1.1 Implement the bounded private projection, account binding, cache and lock.
- [x] 1.2 Connect only successful complete official sync ticks and expose the opt-in CLI/config path.

## 2. Verification and documentation
- [x] 2.1 Run focused synthetic privacy, lifecycle, timestamp, units and failure/cadence tests.
- [x] 2.2 Run the existing synthetic regression suite and record actual results.
- [x] 2.3 Document configuration references and preserve the existing sole OAuth owner.
- [x] 2.4 Revalidate this OpenSpec change strictly; keep live acceptance outside software PASS.

## Actual software verification

- Native Python 3.11, `PYTHONPATH=src python3.11 -m pytest -q`: 103 passed, including 72 focused publisher cases and 31 existing regressions.
- `ruff check .`: all checks passed.
- `git diff --check`: passed.
- `openspec validate add-ha-official-feed --strict`: valid.
- Those tests used synthetic records, private temporary files and a mock HTTP transport. They performed no real WHOOP/HA request, login, refresh, VM, source deployment or runtime change.
- At that initial implementation-only checkpoint, live install, official data arrival, independent HA readback and visible entities remained separate gates; the later actual production acceptance appears below.

## 3. Joint receiver acceptance

- [x] 3.1 Verify the real publisher and complete importer tick against the real isolated HA receiver with a synthetic WHOOP source and fake AW sink.
- [x] 3.2 Independently verify production integration and real official-data arrival through the existing sole Mac owner.

The separate VM lane recorded joint acceptance at 2026-11-04T23:09:46.620217Z
on HA 2026.10.0: 103 unit tests passed; all 23 projected metric fields and 41
stable registered entities passed independent readback and entry reload.
All four simulated provider calls and real importer cursor saves completed; one
real loopback HA publish cleared the private pending cache through the handler
acknowledgement. WHOOP/OAuth provider requests, personal credentials, production
actions and container restarts were zero. Own fixture entries and temporary
files were removed. This is an actual VM receiver/software result using fake
WHOOP measurements, not proof of a production WHOOP API call or personal data.

The unchanged public receipt is
`evidence/2026-11-04-whoop-ha-publisher-vm-joint.public.json`, SHA256
`d27d1022a4e44bf8ddc815d64ae4db97e087e96da0f0ed8dbb0877dd4d40cf4d`.

## Actual natural production acceptance

The unchanged existing owner completed its next ordinary hourly tick at
2026-11-05T00:18:43.451743Z. All four official cursors advanced, its private
cache retained four resources and the HA acknowledgement cleared pending.
The owner stayed at PID 81596/runs 2 throughout 55 read-only observations.
Independent HA registry/status reads found 41 own sensors and a fresh feed;
the separate private HA store readback matched the cache timestamp at full
microsecond precision, with equal provider/source and mode 0600. A separate
SSH readback observed exactly one new HA core process after the earlier
authorized core restart. This monitor issued no restart, retick, POST, provider
request or credential/code change.

The first verifier compared a full-precision cache time to HA's timestamp sensor
display and therefore kept waiting. HA 2026.10.0 intentionally serializes that
sensor in seconds; the displayed second matched and the independent private
store retained the exact microseconds. This was a verifier precision finding,
not a delivery or product failure. The receipt preserves that distinction and
checks both display precision and the exact store separately.

The minimal redacted acceptance receipt is
`evidence/2026-11-05-production-natural-acceptance.public.json`.
See the [joint integration report](https://github.com/Martin-Hausleitner/home-assistant-device-operations/blob/feat/whoop-official-ha-20261104/reports/2026-11-05-whoop-official-home-assistant.md).
This closes the official-feed transport/readback gate; it does not claim a
complete health history, medical interpretation or apartment-wide acceptance.
