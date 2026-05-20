from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime, timedelta
from typing import Any

import httpx

from .config import AW_BASE_URL

BUCKET_PREFIX = "aw-importer-whoop"
EVENT_DATA_SCHEMA_VERSION = 2

SUMMARY_KEYS = {
    "id", "sleep_id", "workout_id", "cycle_id", "recovery_id",
    "start", "end", "start_time", "end_time", "created_at", "updated_at",
    "score_state", "timezone_offset", "timezone", "source",
    "strain", "kilojoule", "energy_kilojoule",
    "average_heart_rate", "average_heart_rate_bpm",
    "max_heart_rate", "max_heart_rate_bpm",
    "sport_id", "nap", "cycle_start", "cycle_end",
    "sleep_performance_percent", "respiratory_rate_rpm", "asleep_duration_min",
    "in_bed_duration_min", "light_sleep_duration_min", "deep_sws_duration_min",
    "rem_duration_min", "activity_name", "duration_min",
    "recovery_score", "recovery_score_percent",
    "resting_heart_rate", "resting_heart_rate_bpm",
    "hrv_rmssd_milli", "heart_rate_variability_ms",
    "spo2_percentage", "blood_oxygen_percent",
    "skin_temp_celsius", "energy_burned_cal",
    "user_calibrating", "question", "question_slug", "answered_yes", "has_notes",
}

SCORE_ALIASES = {
    "strain": ("strain",),
    "kilojoule": ("kilojoule", "energy_kilojoule"),
    "average_heart_rate": ("average_heart_rate", "average_heart_rate_bpm"),
    "max_heart_rate": ("max_heart_rate", "max_heart_rate_bpm"),
    "recovery_score": ("recovery_score", "recovery_score_percent"),
    "resting_heart_rate": ("resting_heart_rate", "resting_heart_rate_bpm"),
    "hrv_rmssd_milli": ("hrv_rmssd_milli", "heart_rate_variability_ms"),
    "spo2_percentage": ("spo2_percentage", "blood_oxygen_percent"),
    "skin_temp_celsius": ("skin_temp_celsius",),
    "user_calibrating": ("user_calibrating",),
}


def stable_hash(record: dict[str, Any]) -> str:
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def record_uuid(record: dict[str, Any]) -> str:
    for key in ("id", "sleep_id", "workout_id", "cycle_id", "recovery_id"):
        if record.get(key):
            return str(record[key])
    raise ValueError(f"WHOOP record has no recognized UUID/id: {record!r}")


def record_times(record: dict[str, Any]) -> tuple[str, float]:
    start = record.get("start") or record.get("start_time") or record.get("created_at")
    end = record.get("end") or record.get("end_time") or record.get("updated_at") or start
    if not start:
        raise ValueError("WHOOP record has no start/start_time/created_at")
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        duration = max(0.0, (b - a).total_seconds())
    except Exception:
        duration = 0.0
    return str(start), duration


def normalized_record(data_type: str, record: dict[str, Any], include_raw: bool = False) -> dict[str, Any]:
    # Keep the online WHOOP API schema compact, but preserve the richer CSV export
    # fields we already normalized in export.py. Journal note text is intentionally
    # not produced by export.py, so this remains safe for ActivityWatch browsing.
    keys = {
        "id", "sleep_id", "workout_id", "cycle_id", "recovery_id",
        "start", "end", "start_time", "end_time", "created_at", "updated_at",
        "score_state", "score", "strain", "average_heart_rate", "max_heart_rate",
        "kilojoule", "sport_id", "timezone_offset", "nap",
        "source", "timezone", "cycle_start", "cycle_end",
        "sleep_performance_percent", "respiratory_rate_rpm", "asleep_duration_min",
        "in_bed_duration_min", "light_sleep_duration_min", "deep_sws_duration_min",
        "rem_duration_min", "activity_name", "duration_min",
        "recovery_score_percent", "resting_heart_rate_bpm",
        "heart_rate_variability_ms", "skin_temp_celsius", "blood_oxygen_percent",
        "energy_burned_cal", "question", "question_slug", "answered_yes", "has_notes",
    }
    data = {k: record[k] for k in keys if k in record}
    data.update(flatten_score(record.get("score")))
    if include_raw:
        data["raw"] = record
    return data


def flatten_score(score: Any) -> dict[str, Any]:
    if not isinstance(score, dict):
        return {}
    data: dict[str, Any] = {}
    for source_key, aliases in SCORE_ALIASES.items():
        if source_key not in score or score[source_key] is None:
            continue
        for alias in aliases:
            data[alias] = score[source_key]
    return data


def event_data_for_record(data_type: str, record: dict[str, Any], include_raw: bool = False) -> dict[str, Any]:
    rid = record_uuid(record)
    normalized = normalized_record(data_type, record, include_raw)
    timestamp, duration = record_times(record)
    data = {
        "whoop_schema_version": EVENT_DATA_SCHEMA_VERSION,
        "whoop_id": rid,
        "data_type": data_type,
        "duration_seconds": duration,
        "duration_minutes": round(duration / 60, 3),
        "duration_hours": round(duration / 3600, 3),
        "record": normalized,
    }
    for key in SUMMARY_KEYS:
        if key in normalized:
            data[key] = normalized[key]
    data.setdefault("start", timestamp)
    remove_empty(data)
    return data


def needs_data_repair(data_type: str, existing_data: dict[str, Any], include_raw: bool = False) -> tuple[bool, dict[str, Any]]:
    record = existing_data.get("record")
    if not isinstance(record, dict):
        return False, existing_data
    repaired = event_data_for_record(data_type, record, include_raw)
    for key, value in existing_data.items():
        if key == "record":
            continue
        repaired.setdefault(key, value)
    repaired["record"] = existing_data["record"]
    return repaired != existing_data, repaired


def remove_empty(data: dict[str, Any]) -> None:
    for key in list(data):
        if data[key] is None:
            del data[key]


class ActivityWatchClient:
    def __init__(self, base_url: str = AW_BASE_URL, timeout: float = 30.0, include_raw: bool = False):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.include_raw = include_raw

    def hostname(self) -> str:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                info = client.get(f"{self.base_url}/info")
                if info.status_code == 200 and info.json().get("hostname"):
                    return str(info.json()["hostname"])
        except Exception:
            pass
        return socket.gethostname()

    def ensure_bucket(self, bucket_id: str, event_type: str) -> None:
        payload = {"client": "aw-importer-whoop", "type": event_type, "hostname": self.hostname()}
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(f"{self.base_url}/buckets/{bucket_id}", json=payload)
            if r.status_code not in (200, 201, 304):
                r.raise_for_status()

    def replace_record_event(self, data_type: str, record: dict[str, Any], old_event_id: int | None = None) -> int:
        bucket_id = f"{BUCKET_PREFIX}-{data_type}"
        self.ensure_bucket(bucket_id, f"whoop.{data_type}")
        rid = record_uuid(record)
        timestamp, duration = record_times(record)
        event = {
            "timestamp": timestamp,
            "duration": duration,
            "data": event_data_for_record(data_type, record, self.include_raw),
        }
        with httpx.Client(timeout=self.timeout) as client:
            stale_ids = [] if old_event_id is None else [old_event_id]
            # If state was lost/clobbered by another sync process, still keep ZIP imports
            # idempotent by finding existing events with the same WHOOP/export id.
            stale_ids.extend(self._find_matching_event_ids(client, bucket_id, rid, timestamp, duration))
            stale_ids = list(dict.fromkeys(stale_ids))

            inserted = client.post(f"{self.base_url}/buckets/{bucket_id}/events", json=event)
            inserted.raise_for_status()
            new_id = inserted.json()
            for event_id in stale_ids:
                if new_id is not None and int(event_id) == int(new_id):
                    continue
                delete = client.delete(f"{self.base_url}/buckets/{bucket_id}/events/{event_id}")
                if delete.status_code not in (200, 404):
                    delete.raise_for_status()
            return int(new_id) if new_id is not None else -1

    def repair_existing_events(self, data_types: tuple[str, ...], days: int = 365, dry_run: bool = False) -> dict[str, int]:
        stats = {"scanned": 0, "updated": 0, "skipped": 0}
        end_dt = datetime.now().astimezone() + timedelta(days=1)
        start_dt = end_dt - timedelta(days=days)
        with httpx.Client(timeout=self.timeout) as client:
            for data_type in data_types:
                bucket_id = f"{BUCKET_PREFIX}-{data_type}"
                r = client.get(
                    f"{self.base_url}/buckets/{bucket_id}/events",
                    params={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
                )
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                for event in r.json():
                    stats["scanned"] += 1
                    data = event.get("data") or {}
                    needs_repair, repaired_data = needs_data_repair(data_type, data, self.include_raw)
                    if not needs_repair:
                        stats["skipped"] += 1
                        continue
                    stats["updated"] += 1
                    if dry_run:
                        continue
                    repaired_event = {
                        "timestamp": event["timestamp"],
                        "duration": event.get("duration", 0),
                        "data": repaired_data,
                    }
                    inserted = client.post(f"{self.base_url}/buckets/{bucket_id}/events", json=repaired_event)
                    inserted.raise_for_status()
                    if event.get("id") is not None:
                        deleted = client.delete(f"{self.base_url}/buckets/{bucket_id}/events/{event['id']}")
                        if deleted.status_code not in (200, 404):
                            deleted.raise_for_status()
        return stats

    def _find_matching_event_ids(
        self,
        client: httpx.Client,
        bucket_id: str,
        whoop_id: str,
        timestamp: str,
        duration: float,
    ) -> list[int]:
        try:
            start_dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) - timedelta(seconds=1)
            end_dt = start_dt + timedelta(seconds=max(duration, 1) + 2)
            r = client.get(
                f"{self.base_url}/buckets/{bucket_id}/events",
                params={"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            )
            if r.status_code != 200:
                return []
            ids: list[int] = []
            for event in r.json():
                data = event.get("data") or {}
                if str(data.get("whoop_id")) == str(whoop_id) and event.get("id") is not None:
                    ids.append(int(event["id"]))
            return ids
        except Exception:
            return []
