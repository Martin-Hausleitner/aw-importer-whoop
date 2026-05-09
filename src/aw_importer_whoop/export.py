from __future__ import annotations

import csv
import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterable

from dateutil import parser as date_parser

from .activitywatch import ActivityWatchClient, stable_hash
from .config import state_path
from .state import ImportState

EXPORT_TYPES = ("sleep", "workout", "cycle", "journal")


@dataclass
class ExportStats:
    parsed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


def _read_zip(path: Path) -> zipfile.ZipFile:
    if zipfile.is_zipfile(path):
        return zipfile.ZipFile(path)
    # WHOOP export links can currently deliver a gzip-wrapped zip while still
    # naming it .zip. Keep this local-only normalization in memory/temp files.
    import gzip

    tmp = TemporaryDirectory()
    normalized = Path(tmp.name) / "whoop-export.zip"
    with gzip.open(path, "rb") as src, normalized.open("wb") as dst:
        dst.write(src.read())
    zf = zipfile.ZipFile(normalized)
    zf._aw_tmpdir = tmp  # type: ignore[attr-defined]  # keep tempdir alive with zipfile
    return zf


def _parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return date_parser.parse(value).isoformat()


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "y"}


def _id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return f"export-{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


def _base(row: dict[str, str]) -> dict[str, Any]:
    return {
        "cycle_start": _parse_dt(row.get("Cycle start time")),
        "cycle_end": _parse_dt(row.get("Cycle end time")),
        "timezone": row.get("Cycle timezone") or None,
        "source": "whoop_export_csv",
    }


def sleep_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Sleep onset")) or _parse_dt(row.get("Cycle start time"))
        end = _parse_dt(row.get("Wake onset")) or _parse_dt(row.get("Cycle end time")) or start
        if not start:
            continue
        record = _base(row) | {
            "sleep_id": _id("sleep", start, end),
            "start": start,
            "end": end,
            "sleep_performance_percent": _float(row.get("Sleep performance %")),
            "respiratory_rate_rpm": _float(row.get("Respiratory rate (rpm)")),
            "asleep_duration_min": _float(row.get("Asleep duration (min)")),
            "in_bed_duration_min": _float(row.get("In bed duration (min)")),
            "light_sleep_duration_min": _float(row.get("Light sleep duration (min)")),
            "deep_sws_duration_min": _float(row.get("Deep (SWS) duration (min)")),
            "rem_duration_min": _float(row.get("REM duration (min)")),
        }
        yield {k: v for k, v in record.items() if v is not None}


def workout_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Workout start time"))
        end = _parse_dt(row.get("Workout end time")) or start
        if not start:
            continue
        record = _base(row) | {
            "workout_id": _id("workout", start, end, row.get("Activity name")),
            "start": start,
            "end": end,
            "activity_name": row.get("Activity name") or None,
            "duration_min": _float(row.get("Duration (min)")),
            "strain": _float(row.get("Activity Strain")),
            "kilojoule": _float(row.get("Energy burned (cal")) or _float(row.get("Energy burned (cal)")),
            "max_heart_rate": _float(row.get("Max HR (bpm)")),
            "average_heart_rate": _float(row.get("Average HR (bpm)")),
        }
        yield {k: v for k, v in record.items() if v is not None}


def cycle_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Cycle start time"))
        end = _parse_dt(row.get("Cycle end time")) or start
        if not start:
            continue
        record = _base(row) | {
            "cycle_id": _id("cycle", start, end),
            "start": start,
            "end": end,
            "recovery_score_percent": _float(row.get("Recovery score %")),
            "resting_heart_rate_bpm": _float(row.get("Resting heart rate (bpm)")),
            "heart_rate_variability_ms": _float(row.get("Heart rate variability (ms)")),
            "skin_temp_celsius": _float(row.get("Skin temp (celsius)")),
            "blood_oxygen_percent": _float(row.get("Blood oxygen %")),
            "strain": _float(row.get("Day Strain")),
            "energy_burned_cal": _float(row.get("Energy burned (cal)")),
            "max_heart_rate": _float(row.get("Max HR (bpm)")),
            "average_heart_rate": _float(row.get("Average HR (bpm)")),
        }
        yield {k: v for k, v in record.items() if v is not None}


def journal_records(rows: Iterable[dict[str, str]]) -> Iterable[dict[str, Any]]:
    for row in rows:
        start = _parse_dt(row.get("Cycle start time"))
        end = _parse_dt(row.get("Cycle end time")) or start
        question = row.get("Question text") or "journal"
        if not start:
            continue
        record = _base(row) | {
            "id": _id("journal", start, question),
            "start": start,
            "end": end,
            "question_text": question,
            "answered_yes": _bool(row.get("Answered yes")),
            # Deliberately do not import free-text notes by default.
            "has_notes": bool(row.get("Notes")),
        }
        yield {k: v for k, v in record.items() if v is not None}


READERS = {
    "sleeps.csv": ("sleep", sleep_records),
    "workouts.csv": ("workout", workout_records),
    "physiological_cycles.csv": ("cycle", cycle_records),
    "journal_entries.csv": ("journal", journal_records),
}


def import_export(path: Path, data_types: tuple[str, ...] = EXPORT_TYPES, dry_run: bool = False) -> ExportStats:
    stats = ExportStats()
    aw = ActivityWatchClient()
    state = ImportState.load(state_path())
    with _read_zip(path) as zf:
        names = set(zf.namelist())
        for filename, (data_type, reader) in READERS.items():
            if data_type not in data_types or filename not in names:
                continue
            with zf.open(filename) as raw:
                rows = csv.DictReader((line.decode("utf-8-sig") for line in raw))
                for record in reader(rows):
                    stats.parsed += 1
                    rid = f"export:{data_type}:{record.get('id') or record.get(data_type + '_id') or record.get('cycle_id')}"
                    h = stable_hash(record)
                    old = state.record_hashes.get(rid)
                    if old == h:
                        stats.skipped += 1
                        continue
                    if not dry_run:
                        new_id = aw.replace_record_event(data_type, record, state.aw_event_ids.get(rid))
                        if new_id >= 0:
                            state.aw_event_ids[rid] = new_id
                        state.record_hashes[rid] = h
                    if old:
                        stats.updated += 1
                    else:
                        stats.inserted += 1
    if not dry_run:
        state.save(state_path())
    return stats
