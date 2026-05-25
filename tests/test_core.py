from __future__ import annotations

from datetime import datetime, timezone
from zipfile import ZipFile

from aw_importer_whoop.activitywatch import event_data_for_record, needs_data_repair, normalized_record, record_times, record_uuid, stable_hash
from aw_importer_whoop.config import SCOPES
from aw_importer_whoop.export import cycle_records, import_export, journal_records, sleep_records, workout_records
from aw_importer_whoop.state import ImportState, parse_dt
from aw_importer_whoop.sync import SyncLoop


def test_stable_hash_is_order_independent() -> None:
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})


def test_oauth_scopes_are_minimal_for_imported_data() -> None:
    assert "read:profile" not in SCOPES
    assert "read:body_measurement" not in SCOPES


def test_record_uuid_accepts_whoop_ids() -> None:
    assert record_uuid({"sleep_id": "123"}) == "123"
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
    tokens = tmp_path / "tokens.json"
    tokens.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("aw_importer_whoop.sync.token_path", lambda: tokens)
    monkeypatch.setattr("aw_importer_whoop.sync.state_path", lambda: tmp_path / "state.json")
    loop = SyncLoop("id", "secret", data_types=("sleep",))
    loop.state.last_successful_sync_per_type["sleep"] = "2026-05-08T12:00:00+00:00"
    assert loop._start_for("sleep") == datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)


def test_parse_dt_accepts_z_suffix() -> None:
    assert parse_dt("2026-05-08T12:00:00Z") == datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)


def test_export_sleep_record_maps_interval() -> None:
    rec = next(iter(sleep_records([{
        "Cycle start time": "2026-05-01T00:00:00Z",
        "Cycle end time": "2026-05-01T08:00:00Z",
        "Sleep onset": "2026-05-01T00:30:00Z",
        "Wake onset": "2026-05-01T07:30:00Z",
        "Sleep performance %": "85",
    }])))
    assert rec["start"] == "2026-05-01T00:30:00+00:00"
    assert rec["end"] == "2026-05-01T07:30:00+00:00"
    assert rec["sleep_performance_percent"] == 85


def test_export_workout_record_maps_activity() -> None:
    rec = next(iter(workout_records([{
        "Workout start time": "2026-05-01T12:00:00Z",
        "Workout end time": "2026-05-01T13:00:00Z",
        "Activity name": "Running",
        "Activity Strain": "12.3",
        "Energy burned (cal)": "456",
    }])))
    assert rec["activity_name"] == "Running"
    assert rec["strain"] == 12.3
    assert rec["energy_burned_cal"] == 456


def test_export_cycle_record_maps_recovery_metrics() -> None:
    rec = next(iter(cycle_records([{
        "Cycle start time": "2026-05-01T00:00:00Z",
        "Cycle end time": "2026-05-02T00:00:00Z",
        "Recovery score %": "67",
        "Resting heart rate (bpm)": "55",
        "Heart rate variability (ms)": "72.5",
        "Blood oxygen %": "98",
    }])))
    assert rec["recovery_score_percent"] == 67
    assert rec["resting_heart_rate_bpm"] == 55
    assert rec["heart_rate_variability_ms"] == 72.5
    assert rec["blood_oxygen_percent"] == 98


def test_journal_records_do_not_include_private_notes() -> None:
    rec = next(iter(journal_records([{
        "Cycle start time": "2026-05-01T00:00:00Z",
        "Cycle end time": "2026-05-01T08:00:00Z",
        "Question text": "Did you drink alcohol?",
        "Answered yes": "yes",
        "Notes": "private note",
    }])))
    assert rec["question"] == "Did you drink alcohol?"
    assert rec["question_slug"] == "did-you-drink-alcohol"
    assert "Notes" not in rec
    assert "notes" not in rec
    assert rec["has_notes"] is True


def test_normalized_record_keeps_export_metrics_without_raw_notes() -> None:
    data = normalized_record("sleep", {
        "sleep_id": "s1",
        "source": "whoop_export_csv",
        "sleep_performance_percent": 91,
        "Notes": "private note",
    })
    assert data["sleep_performance_percent"] == 91
    assert "Notes" not in data
    assert "raw" not in data


def test_normalized_record_flattens_cycle_score_metrics() -> None:
    data = normalized_record("cycle", {
        "id": 123,
        "start": "2026-05-20T01:00:00Z",
        "end": "2026-05-21T00:00:00Z",
        "score_state": "SCORED",
        "score": {
            "strain": 10.5,
            "kilojoule": 6339.9,
            "average_heart_rate": 73,
            "max_heart_rate": 176,
        },
    })

    assert data["strain"] == 10.5
    assert data["kilojoule"] == 6339.9
    assert data["energy_kilojoule"] == 6339.9
    assert data["average_heart_rate"] == 73
    assert data["average_heart_rate_bpm"] == 73
    assert data["max_heart_rate"] == 176
    assert data["max_heart_rate_bpm"] == 176


def test_event_data_for_record_adds_queryable_top_level_fields() -> None:
    data = event_data_for_record("cycle", {
        "id": 123,
        "start": "2026-05-20T01:00:00Z",
        "end": "2026-05-21T00:00:00Z",
        "score_state": "SCORED",
        "score": {
            "strain": 10.5,
            "kilojoule": 6339.9,
            "average_heart_rate": 73,
            "max_heart_rate": 176,
        },
    })

    assert data["whoop_schema_version"] == 2
    assert data["whoop_id"] == "123"
    assert data["data_type"] == "cycle"
    assert data["start"] == "2026-05-20T01:00:00Z"
    assert data["end"] == "2026-05-21T00:00:00Z"
    assert data["duration_hours"] == 23
    assert data["strain"] == 10.5
    assert data["energy_kilojoule"] == 6339.9
    assert data["average_heart_rate_bpm"] == 73
    assert data["record"]["score"]["strain"] == 10.5


def test_needs_data_repair_detects_missing_top_level_fields() -> None:
    old_data = {
        "whoop_id": "123",
        "data_type": "cycle",
        "record": {
            "id": 123,
            "start": "2026-05-20T01:00:00Z",
            "end": "2026-05-21T00:00:00Z",
            "score": {"strain": 10.5},
        },
    }

    needs_repair, repaired = needs_data_repair("cycle", old_data)

    assert needs_repair is True
    assert repaired["whoop_schema_version"] == 2
    assert repaired["strain"] == 10.5
    assert repaired["record"] == old_data["record"]


def test_import_export_dry_run_reads_zip_without_activitywatch_or_state(monkeypatch, tmp_path) -> None:
    zip_path = tmp_path / "whoop-export.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "sleeps.csv",
            "Cycle start time,Cycle end time,Sleep onset,Wake onset,Sleep performance %\n"
            "2026-05-01T00:00:00Z,2026-05-01T08:00:00Z,2026-05-01T00:30:00Z,2026-05-01T07:30:00Z,88\n",
        )

    state_file = tmp_path / "state.json"
    monkeypatch.setattr("aw_importer_whoop.export.state_path", lambda: state_file)

    stats = import_export(zip_path, ("sleep",), dry_run=True)

    assert stats.parsed == 1
    assert stats.inserted == 1
    assert stats.updated == 0
    assert stats.skipped == 0
    assert not state_file.exists()
