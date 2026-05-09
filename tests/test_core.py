from __future__ import annotations

from datetime import datetime, timezone

from aw_importer_whoop.activitywatch import record_times, record_uuid, stable_hash
from aw_importer_whoop.export import journal_records, sleep_records, workout_records
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


def test_export_sleep_record_maps_interval() -> None:
    rows = [{"Cycle start time": "2026-05-01T00:00:00Z", "Cycle end time": "2026-05-01T08:00:00Z", "Sleep onset": "2026-05-01T00:30:00Z", "Wake onset": "2026-05-01T07:30:00Z", "Sleep performance %": "85"}]
    rec = next(iter(sleep_records(rows)))
    assert rec["start"] == "2026-05-01T00:30:00+00:00"
    assert rec["end"] == "2026-05-01T07:30:00+00:00"
    assert rec["sleep_performance_percent"] == 85


def test_export_workout_record_maps_activity() -> None:
    rows = [{"Workout start time": "2026-05-01T12:00:00Z", "Workout end time": "2026-05-01T13:00:00Z", "Activity name": "Running", "Activity Strain": "12.3"}]
    rec = next(iter(workout_records(rows)))
    assert rec["activity_name"] == "Running"
    assert rec["strain"] == 12.3


def test_journal_records_do_not_include_private_text_or_notes() -> None:
    rows = [{"Cycle start time": "2026-05-01T00:00:00Z", "Cycle end time": "2026-05-01T08:00:00Z", "Question text": "Did you drink alcohol?", "Answered yes": "yes", "Notes": "private note"}]
    rec = next(iter(journal_records(rows)))
    assert "question_text" not in rec
    assert "Notes" not in rec
    assert "notes" not in rec
    assert rec["question_hash"]
    assert rec["question_slug"] == "did-you-drink-alcohol"
    assert rec["has_notes"] is True
