from __future__ import annotations

import hashlib
import json
import socket
from datetime import datetime
from typing import Any

import httpx

from .config import AW_BASE_URL

BUCKET_PREFIX = "aw-importer-whoop"


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
    keys = {
        "id", "sleep_id", "workout_id", "cycle_id", "recovery_id",
        "start", "end", "start_time", "end_time", "created_at", "updated_at",
        "score_state", "score", "strain", "average_heart_rate", "max_heart_rate",
        "kilojoule", "sport_id", "timezone_offset", "nap",
    }
    data = {k: record[k] for k in keys if k in record}
    if include_raw:
        data["raw"] = record
    return data


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
            "data": {
                "whoop_id": rid,
                "data_type": data_type,
                "record": normalized_record(data_type, record, self.include_raw),
            },
        }
        with httpx.Client(timeout=self.timeout) as client:
            inserted = client.post(f"{self.base_url}/buckets/{bucket_id}/events", json=event)
            inserted.raise_for_status()
            new_id = inserted.json()
            if old_event_id is not None:
                delete = client.delete(f"{self.base_url}/buckets/{bucket_id}/events/{old_event_id}")
                if delete.status_code not in (200, 404):
                    delete.raise_for_status()
            return int(new_id) if new_id is not None else -1
