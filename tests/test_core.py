from __future__ import annotations

from datetime import datetime, timezone

from aw_importer_whoop.activitywatch import record_times, record_uuid, stable_hash
from aw_importer_whoop.state import ImportState, parse_dt
from aw_importer_whoop.sync import SyncLoop


def test_stable_hash_is_order_independent() -> None:
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_record_uuid_accepts_whoop_ids() -> None:
    assert record_uuid({"sleep_id": 123}) == "123"
    assert record_uuid({"id": "abc"}) == "abc"


def test_record_times_uses_duration_seconds() -> None:
    timestamp, duration = record_times({"start": "2026-05-08T10:00:00+00:00", "end": "2026-05-08T11:30:00+00:00"})
    assert timestamp == "2026-05-08T10:00:00+00:00"
    assert duration == 5400


def test_state_roundtrip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = ImportState(last_successful_sync_per_type={"sleep": "x"}, record_hashes={"sleep:1": "h"})
    state.save(path)
    loaded = ImportState.load(path)
    assert loaded.last_successful_sync_per_type == {"sleep": "x"}
    assert loaded.record_hashes == {"sleep:1": "h"}


def test_sync_start_uses_six_hour_overlap(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("aw_importer_whoop.sync.token_path", lambda: tmp_path / "tokens.json")
    monkeypatch.setattr("aw_importer_whoop.sync.state_path", lambda: tmp_path / "state.json")
    loop = SyncLoop("id", "secret", data_types=("sleep",))
    loop.state.last_successful_sync_per_type["sleep"] = "2026-05-08T12:00:00+00:00"
    assert loop._start_for("sleep") == datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)


def test_parse_dt_accepts_z_suffix() -> None:
    assert parse_dt("2026-05-08T12:00:00Z") == datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
